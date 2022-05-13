from functools import partial
from typing import Callable, Dict, Optional, Tuple

import jax
import jax.numpy as jnp
import numpyro
import numpyro.distributions as dist
from jax import jit

from .gp import ExactGP
from .kernels import get_kernel


class DKL(ExactGP):
    """
    Fully Bayesian implementation of deep kernel learning

    Args:
        input_dim: number of input dimensions
        z_dim: latent space dimensionality
        kernel: type of kernel ('RBF', 'Matern', 'Periodic')
        kernel_prior: optional priors over kernel hyperparameters (uses LogNormal(0,1) by default)
        bnn_fn: Custom MLP
        bnn_fn_prior: Bayesian priors over the weights and biases in bnn_fn
        latent_prior: Optional prior over the latent space (BNN embedding)
    """

    def __init__(self, input_dim: int, z_dim: int = 2, kernel: str = 'RBF',
                 kernel_prior: Optional[Callable[[], Dict[str, jnp.ndarray]]] = None,
                 gp_mean_fn: Optional[Callable[[jnp.ndarray, Dict[str, jnp.ndarray]], jnp.ndarray]] = None,
                 gp_mean_fn_prior: Optional[Callable[[], Dict[str, jnp.ndarray]]] = None,
                 bnn_fn: Optional[Callable[[jnp.ndarray, Dict[str, jnp.ndarray]], jnp.ndarray]] = None,
                 bnn_fn_prior: Optional[Callable[[], Dict[str, jnp.ndarray]]] = None,
                 latent_prior: Optional[Callable[[jnp.ndarray], Dict[str, jnp.ndarray]]] = None
                 ) -> None:
        super(DKL, self).__init__(input_dim, kernel, kernel_prior)
        self.bnn = bnn_fn if bnn_fn else bnn
        self.bnn_prior = bnn_fn_prior if bnn_fn_prior else bnn_prior(input_dim, z_dim)
        self.kernel_dim = z_dim
        self.latent_prior = latent_prior
        self.gp_mean_fn = gp_mean_fn
        self.gp_mean_fn_prior = gp_mean_fn_prior

    def model(self, X: jnp.ndarray, y: jnp.ndarray) -> None:
        """DKL probabilistic model"""
        # BNN part
        bnn_params = self.bnn_prior()
        z = self.bnn(X, bnn_params)
        # Add GP prior mean function (if any)
        f_loc = jnp.zeros(z.shape[0])
        if self.gp_mean_fn is not None:
            args = [z]
            if self.gp_mean_fn_prior is not None:
                args += [self.gp_mean_fn_prior()]
            f_loc += self.gp_mean_fn(*args).squeeze()
        if self.latent_prior:  # Sample latent variable
            z = self.latent_prior(z)
        # Sample GP kernel parameters
        if self.kernel_prior:
            kernel_params = self.kernel_prior()
        else:
            kernel_params = self._sample_kernel_params()
        # Sample noise
        noise = numpyro.sample("noise", dist.LogNormal(0.0, 1.0))
        # compute kernel
        k = get_kernel(self.kernel)(
            z, z,
            kernel_params,
            noise
        )
        # sample y according to the standard Gaussian process formula
        numpyro.sample(
            "y",
            dist.MultivariateNormal(loc=f_loc, covariance_matrix=k),
            obs=y,
        )

    @partial(jit, static_argnames='self')
    def get_mvn_posterior(self,
                          X_new: jnp.ndarray, params: Dict[str, jnp.ndarray],
                          noiseless: bool = False
                          ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        """
        Returns parameters (mean and cov) of multivariate normal posterior
        for a single sample of DKL hyperparameters
        """
        noise = params["noise"]
        noise_p = noise * (1 - jnp.array(noiseless, int))
        # embed data intot the latent space
        z_train = self.bnn(self.X_train, params)
        z_test = self.bnn(X_new, params)
        # Mean function
        y_residual = self.y_train
        if self.gp_mean_fn is not None:
            args = [z_train, params] if self.gp_mean_fn_prior else [z_train]
            y_residual -= self.gp_mean_fn(*args).squeeze()
        # compute kernel matrices for train and test data
        k_pp = get_kernel(self.kernel)(z_test, z_test, params, noise_p)
        k_pX = get_kernel(self.kernel)(z_test, z_train, params, jitter=0)
        k_XX = get_kernel(self.kernel)(z_train, z_train, params, noise)
        # compute the predictive covariance and mean
        K_xx_inv = jnp.linalg.inv(k_XX)
        cov = k_pp - jnp.matmul(k_pX, jnp.matmul(K_xx_inv, jnp.transpose(k_pX)))
        mean = jnp.matmul(k_pX, jnp.matmul(K_xx_inv, y_residual))
        if self.gp_mean_fn is not None:
            args = [z_test, params] if self.gp_mean_fn_prior else [z_test]
            mean += self.gp_mean_fn(*args).squeeze()
        return mean, cov

    @partial(jit, static_argnames='self')
    def embed(self, X_new: jnp.ndarray) -> jnp.ndarray:
        samples = self.get_samples(chain_dim=False)
        predictive = jax.vmap(lambda params: self.bnn(X_new, params))
        z = predictive(samples)
        return z


def sample_weights(name: str, in_channels: int, out_channels: int) -> jnp.ndarray:
    """Sampling weights matrix"""
    return numpyro.sample(name=name, fn=dist.Normal(
                loc=jnp.zeros((in_channels, out_channels)),
                scale=jnp.ones((in_channels, out_channels))))


def sample_biases(name: str, channels: int) -> jnp.ndarray:
    """Sampling bias vector"""
    return numpyro.sample(name=name, fn=dist.Normal(
                loc=jnp.zeros((channels)), scale=jnp.ones((channels))))


def bnn(X: jnp.ndarray, params: Dict[str, jnp.ndarray]) -> jnp.ndarray:
    """Simple Bayesian MLP"""
    h1 = jnp.tanh(jnp.matmul(X, params["w1"]) + params["b1"])
    h2 = jnp.tanh(jnp.matmul(h1, params["w2"]) + params["b2"])
    z = jnp.matmul(h2, params["w3"]) + params["b3"]
    return z


def bnn_prior(input_dim: int, zdim: int = 2) -> Dict[str, jnp.array]:
    """Priors over weights and biases in the default Bayesian MLP"""
    hdim = [64, 32]

    def _bnn_prior():
        w1 = sample_weights("w1", input_dim, hdim[0])
        b1 = sample_biases("b1", hdim[0])
        w2 = sample_weights("w2", hdim[0], hdim[1])
        b2 = sample_biases("b2", hdim[1])
        w3 = sample_weights("w3", hdim[1], zdim)
        b3 = sample_biases("b3", zdim)
        return {"w1": w1, "b1": b1, "w2": w2, "b2": b2, "w3": w3, "b3": b3}

    return _bnn_prior
