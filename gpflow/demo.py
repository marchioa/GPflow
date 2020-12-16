from typing import Tuple

import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp

import gpflow
from gpflow import Parameter
from gpflow import covariances
from gpflow import kullback_leiblers
# from gpflow.conditionals import conditional
from gpflow.config import default_float, default_jitter
from gpflow.models.model import GPModel, InputData, MeanAndVariance, RegressionData
from gpflow.models.training_mixins import ExternalDataTrainingLossMixin
from gpflow.models.util import inducingpoint_wrapper
from gpflow.utilities import positive, triangular
from gpflow.conditionals.util import mix_latent_gp, expand_independent_outputs


class DiagNormal(gpflow.Module):
    def __init__(self, q_mu, q_sqrt):
        self.q_mu = gpflow.Parameter(q_mu)  # [M, L]
        self.q_sqrt = gpflow.Parameter(q_sqrt)  # [M, L]


class MvnNormal(gpflow.Module):
    def __init__(self, q_mu, q_sqrt):
        self.q_mu = gpflow.Parameter(q_mu)  # [M, L]
        self.q_sqrt = gpflow.Parameter(q_sqrt, transform=gpflow.utilities.triangular())  # [L, M, M]


def eye_like(A):
    return tf.eye(tf.shape(A)[-1], dtype=A.dtype)


"""
@dispatch
def cond_precompute(kernel, iv, q_mu, q_sqrt, whiten):
    pass

@dispatch
def cond_execute(Xnew, kernel, iv, alpha, Qinv):
    pass


@dispatch
def conditional(kernel, iv, q_mu, q_sqrt, whiten):
    # precompute
    def execute(Xnew, full_cov):
        pass
    return execute
"""

class Posterior:
    def __init__(self, kernel, iv, q_dist, whiten=True, mean_function=None):
        self.iv = iv
        self.kernel = kernel
        self.q_dist = q_dist
        self.mean_function = mean_function
        self.whiten = whiten

        self._precompute()  # populates self.alpha and self.Qinv

    def freeze(self):
        """
        Note- this simply cuts the computational graph
        """
        self.alpha = tf.constant(self.alpha.numpy())
        self.Qinv = tf.constant(self.Qinv.numpy())

    def predict_f(
        self, Xnew, full_cov: bool = False, full_output_cov: bool = False
    ) -> MeanAndVariance:
        # Qinv: [L, M, M]
        # alpha: [M, L]

        Kuf = covariances.Kuf(self.iv, self.kernel, Xnew)  # [(R), M, N]
        mean = tf.matmul(Kuf, self.alpha, transpose_a=True)
        if Kuf.shape.ndims == 3:
            mean = tf.einsum("...rn->...nr", tf.squeeze(mean, axis=-1))

        if isinstance(self.kernel,
                      (gpflow.kernels.SeparateIndependent, gpflow.kernels.IndependentLatent)):
             
            Knn = tf.stack([k(Xnew, full_cov=full_cov) for k in self.kernel.kernels], axis=0)
        elif isinstance(self.kernel, gpflow.kernels.MultioutputKernel):
            Knn = self.kernel.kernel(Xnew, full_cov=full_cov)
        else:
            Knn = self.kernel(Xnew, full_cov=full_cov)

        if full_cov:
            Kfu_Qinv_Kuf = tf.matmul(Kuf, tf.matmul(self.Qinv, Kuf), transpose_a=True)
            cov = Knn - Kfu_Qinv_Kuf
        else:
            # [AT B]_ij = AT_ik B_kj = A_ki B_kj
            # TODO check whether einsum is faster now?
            Kfu_Qinv_Kuf = tf.reduce_sum(Kuf * tf.matmul(self.Qinv, Kuf), axis=-2)
            cov = Knn - Kfu_Qinv_Kuf
            cov = tf.linalg.adjoint(cov)

        if isinstance(self.kernel, gpflow.kernels.LinearCoregionalization):
            cov = expand_independent_outputs(cov, full_cov, full_output_cov=False)
            mean, cov = mix_latent_gp(self.kernel.W, mean, cov, full_cov, full_output_cov)
        else:
            cov = expand_independent_outputs(cov, full_cov, full_output_cov)

        return mean + self.mean_function(Xnew), cov

    def _precompute(self):
        Kuu = covariances.Kuu(self.iv, self.kernel, jitter=default_jitter())  # [(R), M, M]
        L = tf.linalg.cholesky(Kuu)

        q_mu = self.q_dist.q_mu
        if Kuu.shape.ndims == 3:
            q_mu = tf.einsum("...mr->...rm", self.q_dist.q_mu)[..., None]  # [..., R, M, 1]

        if not self.whiten:
            # alpha = Kuu⁻¹ q_mu
            alpha = tf.linalg.cholesky_solve(L, q_mu)
        else:
            # alpha = L⁻T q_mu
            alpha = tf.linalg.triangular_solve(L, q_mu, adjoint=True)
        # predictive mean = Kfu alpha
        # predictive variance = Kff - Kfu Qinv Kuf
        # S = q_sqrt q_sqrtT
        if not self.whiten:
            # Qinv = Kuu⁻¹ - Kuu⁻¹ S Kuu⁻¹
            #      = Kuu⁻¹ - L⁻T L⁻¹ S L⁻T L⁻¹
            #      = L⁻T (I - L⁻¹ S L⁻T) L⁻¹
            #      = L⁻T B L⁻¹
            if isinstance(self.q_dist, DiagNormal):
                q_sqrt = tf.linalg.diag(tf.linalg.adjoint(self.q_dist.q_sqrt))
            else:
                q_sqrt = self.q_dist.q_sqrt
            Linv_qsqrt = tf.linalg.triangular_solve(L, q_sqrt)
            Linv_cov_u_LinvT = tf.matmul(Linv_qsqrt, Linv_qsqrt, transpose_b=True)
        else:
            if isinstance(self.q_dist, DiagNormal):
                Linv_cov_u_LinvT = tf.linalg.diag(tf.linalg.adjoint(self.q_dist.q_sqrt ** 2))
            else:
                q_sqrt = self.q_dist.q_sqrt
                Linv_cov_u_LinvT = tf.matmul(q_sqrt, q_sqrt, transpose_b=True)
            # Qinv = Kuu⁻¹ - L⁻T S L⁻¹
            # Linv = (L⁻¹ I) = solve(L, I)
            # Kinv = Linv.T @ Linv
        I = eye_like(Linv_cov_u_LinvT)
        B = I - Linv_cov_u_LinvT
        LinvT_B = tf.linalg.triangular_solve(L, B, adjoint=True)
        B_Linv = tf.linalg.adjoint(LinvT_B)
        Qinv = tf.linalg.triangular_solve(L, B_Linv, adjoint=True)
        self.alpha = Parameter(alpha, trainable=False)
        self.Qinv = Parameter(Qinv, trainable=False)


class NewSVGP(GPModel, ExternalDataTrainingLossMixin):
    """
    Differences from gpflow.models.SVGP:
    - q_dist instead of q_mu/q_sqrt
    - posterior() method
    """

    def __init__(
        self,
        kernel,
        likelihood,
        inducing_variable,
        *,
        mean_function=None,
        num_latent_gps: int = 1,
        q_diag: bool = False,
        q_mu=None,
        q_sqrt=None,
        whiten: bool = True,
        num_data=None,
    ):
        super().__init__(kernel, likelihood, mean_function, num_latent_gps)
        self.num_data = num_data
        self.q_diag = q_diag
        self.whiten = whiten
        self.inducing_variable = inducingpoint_wrapper(inducing_variable)

        # init variational parameters
        num_inducing = self.inducing_variable.num_inducing
        self.q_dist = self._init_variational_parameters(num_inducing, q_mu, q_sqrt, q_diag)

    def posterior(self, freeze=False):
        """
        If freeze=True, cuts the computational graph after precomputing alpha and Qinv
        this works around some issues in the tensorflow graph optimisation and gives much
        faster prediction when wrapped inside tf.function()
        """
        posterior = Posterior(
            self.kernel,
            self.inducing_variable,
            self.q_dist,
            whiten=self.whiten,
            mean_function=self.mean_function,
        )
        if freeze:
            posterior.freeze()
        return posterior

    def predictor(self):
        return conditional_closure(
            self.kernel,
            self.inducing_variable,
            self.q_dist,
            whiten=self.whiten,
            mean_function=self.mean_function,
        )

    def _init_variational_parameters(self, num_inducing, q_mu, q_sqrt, q_diag):
        q_mu = np.zeros((num_inducing, self.num_latent_gps)) if q_mu is None else q_mu
        q_mu = Parameter(q_mu, dtype=default_float())  # [M, P]

        if q_diag:
            if q_sqrt is None:
                q_sqrt = np.ones((num_inducing, self.num_latent_gps), dtype=default_float())
            else:
                assert q_sqrt.ndim == 2
                self.num_latent_gps = q_sqrt.shape[1]
            q_sqrt = Parameter(q_sqrt, transform=positive())  # [M, L|P]
            return DiagNormal(q_mu, q_sqrt)
        else:
            if q_sqrt is None:
                q_sqrt = np.array(
                    [
                        np.eye(num_inducing, dtype=default_float())
                        for _ in range(self.num_latent_gps)
                    ]
                )
            else:
                assert q_sqrt.ndim == 3
                self.num_latent_gps = q_sqrt.shape[0]
                num_inducing = q_sqrt.shape[1]
            q_sqrt = Parameter(q_sqrt, transform=triangular())  # [L|P, M, M]
            return MvnNormal(q_mu, q_sqrt)

    def prior_kl(self) -> tf.Tensor:
        return kullback_leiblers.prior_kl(
            self.inducing_variable,
            self.kernel,
            self.q_dist.q_mu,
            self.q_dist.q_sqrt,
            whiten=self.whiten,
        )

    def maximum_log_likelihood_objective(self, data: RegressionData) -> tf.Tensor:
        return self.elbo(data)

    def elbo(self, data: RegressionData) -> tf.Tensor:
        """
        This gives a variational bound (the evidence lower bound or ELBO) on
        the log marginal likelihood of the model.
        """
        X, Y = data
        kl = self.prior_kl()
        # f_mean, f_var = self.posterior().predict_f(X, full_cov=False, full_output_cov=False)
        f_mean, f_var = self.predictor()(X, full_cov=False, full_output_cov=False)
        var_exp = self.likelihood.variational_expectations(f_mean, f_var, Y)
        if self.num_data is not None:
            num_data = tf.cast(self.num_data, kl.dtype)
            minibatch_size = tf.cast(tf.shape(X)[0], kl.dtype)
            scale = num_data / minibatch_size
        else:
            scale = tf.cast(1.0, kl.dtype)
        return tf.reduce_sum(var_exp) * scale - kl

    def predict_f(self, Xnew: InputData, full_cov=False, full_output_cov=False) -> MeanAndVariance:
        """
        For backwards compatibility
        For fast prediction, get a posterior object first: model.posterior() -- see freeze argument
        then do posterior.predict_f(Xnew...)
        """
        return self.posterior(freeze=False).predict_f(
        # return self.predictor()(
            Xnew, full_cov=full_cov, full_output_cov=full_output_cov
        )


"""
# Option 1: recomputing from scratch
m = SVGP(...)
# optimize model
p1 = m.posterior(freeze=True)
p2 = m.posterior(freeze=True)  # recomputed - we don't want that
# optimize some more
p3 = m.posterior(freeze=True)  # recomputes - we want that
p4 = m.posterior(freeze=True)  # recomputes as well - we don't want that


# Option 2: caching directly
m = SVGP(...)
# optimize model
p1 = m.posterior(freeze=True)
p2 = m.posterior(freeze=True)  # does not recompute - we want that
# optimize some more
p3 = m.posterior(freeze=True)  # still the old version!? - we don't want that

property:
m.predict_f # already does the computation

predfn = m.predict_f # -> function object
predfn()

^ v equivalent

m.predict_f()
"""


def make_models(M=64, D=5, L=3, q_diag=False, whiten=True, mo=None):
    if mo is None:
        k = gpflow.kernels.Matern52()
        Z = np.random.randn(M, D)
    else:
        kernel_type, iv_type = mo

        k_list = [gpflow.kernels.Matern52() for _ in range(L)]
        w = tf.Variable(initial_value=np.random.rand(2, L), dtype=tf.float64, name='w')
        if kernel_type == "LinearCoregionalization":
            k = gpflow.kernels.LinearCoregionalization(k_list, W=w)
        elif kernel_type == "SeparateIndependent":
            k = gpflow.kernels.SeparateIndependent(k_list)
        elif kernel_type == "SharedIndependent":
            k = gpflow.kernels.SharedIndependent(k_list[0], output_dim=2)
        iv_list = [gpflow.inducing_variables.InducingPoints(np.random.randn(M, D)) for _ in range(L)]
        if iv_type == "SeparateIndependent":
            Z = gpflow.inducing_variables.SeparateIndependentInducingVariables(iv_list)
        elif iv_type == "SharedIndependent":
            Z = gpflow.inducing_variables.SharedIndependentInducingVariables(iv_list[0])
    lik = gpflow.likelihoods.Gaussian(0.1)
    q_mu = np.random.randn(M, L)
    if q_diag:
        q_sqrt = np.random.randn(M, L)**2
    else:
        q_sqrt = np.tril(np.random.randn(L, M, M))
    mold = gpflow.models.SVGP(k, lik, Z, q_diag=q_diag, q_mu=q_mu, q_sqrt=q_sqrt, whiten=whiten)
    mnew = NewSVGP(k, lik, Z, q_diag=q_diag, q_mu=q_mu, q_sqrt=q_sqrt, whiten=whiten)
    return mold, mnew

# TODO: compare timings for q_diag=True, whiten=False, ...
mold, mnew = make_models(q_diag=False, mo=None)
X = np.random.randn(100, 5)
Xt = tf.convert_to_tensor(X)
pred_old = tf.function(mold.predict_f)

pred_newfrozen = tf.function(mnew.posterior(freeze=True).predict_f)
pred_new = tf.function(mnew.posterior(freeze=False).predict_f)
def predict_f_once(Xnew):
    return mnew.posterior().predict_f(Xnew)
pred_new_once = tf.function(predict_f_once)

# pred_new = tf.function(mnew.predictor())
# %timeit pred_old(Xt)
# %timeit pred_new(Xt)


def test_correct():
    from itertools import product
    conf = product(*[(True, False)] * 3)
    for q_diag, white, use_mo in conf:
        if use_mo:
            mo_options = list(product(
                ("LinearCoregionalization", "SeparateIndependent", "SharedIndependent"),
                ("SeparateIndependent", "SharedIndependent"),
            ))
        else:
            mo_options = [None]
        for mo in mo_options:
            print(f"Testing q_diag={q_diag} white={white} mo={mo}...")
            mold, mnew = make_models(q_diag=q_diag, whiten=white, mo=mo)

            mu, var = mnew.predict_f(X, full_cov=True, full_output_cov=True)
            mu2, var2 = mold.predict_f(X, full_cov=True, full_output_cov=True)
            np.testing.assert_allclose(mu, mu2)
            np.testing.assert_allclose(var, var2)
            mu, var = mnew.predict_f(X, full_cov=True, full_output_cov=False)
            mu2, var2 = mold.predict_f(X, full_cov=True, full_output_cov=False)
            np.testing.assert_allclose(mu, mu2)
            np.testing.assert_allclose(var, var2)
            mu, var = mnew.predict_f(X, full_cov=False, full_output_cov=True)
            mu2, var2 = mold.predict_f(X, full_cov=False, full_output_cov=True)
            np.testing.assert_allclose(mu, mu2)
            np.testing.assert_allclose(var, var2)
            mu, var = mnew.predict_f(X, full_cov=False, full_output_cov=False)
            mu2, var2 = mold.predict_f(X, full_cov=False, full_output_cov=False)
            np.testing.assert_allclose(mu, mu2)
            np.testing.assert_allclose(var, var2)


test_correct()