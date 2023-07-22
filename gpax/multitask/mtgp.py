from typing import Callable, Dict, Optional

import jax.numpy as jnp
import numpy as onp
import numpyro
import numpyro.distributions as dist

from ..gp import ExactGP
from ..kernels import LCMKernel


class MultiTaskGP(ExactGP):
    """
    Gaussian process for multi-task/fidelity learning

    Args:
        input_dim:
            Number of input dimensions
        data_kernel:
            Kernel function operating on data inputs ('RBF', 'Matern', 'Periodic', or a custom function)
        num_latents:
            Number of latent functions. Typically equal to or less than the number of tasks
        shared_input_space:
            If True (default), assumes that all tasks share the same input space and
            uses a multivariate kernel (Kronecker product). If False, assumes that different tasks
            have different number of observations and uses a multitask kernel (elementwise multiplication).
            In that case, the task indices must be appended as the last column of the input vector.
        num_tasks:
            Number of tasks. This is only needed if `shared_input_space` is True.
        rank:
            Rank of the weight matrix in the task kernel. Cannot be larger than the number of tasks.
            Higher rank implies higher correlation. Uses *(num_tasks - 1)* when not specified.
        mean_fn:
            Optional deterministic mean function (use 'mean_fn_priors' to make it probabilistic)
        data_kernel_prior:
            Optional custom priors over the data kernel hyperparameters; uses LogNormal(0,1) by default
        mean_fn_prior:
            Optional priors over mean function parameters
        noise_prior:
            Optional custom prior for observational noise; uses LogNormal(0,1) by default.
        task_kernel_prior:
            Optional custom priors over task kernel parameters;
            Defaults to Normal(0, 10) for weights W and LogNormal(0, 1) for variances v.
        output_scale:
            Option to sample data kernel's output scale.
            Defaults to False to avoid over-parameterization (the scale is already absorbed into task kernel)
    """
    def __init__(self, input_dim: int, data_kernel: str,
                 num_latents: int = None, shared_input_space: bool = True,
                 num_tasks: int = None, rank: Optional[int] = None,
                 mean_fn: Optional[Callable[[jnp.ndarray, Dict[str, jnp.ndarray]], jnp.ndarray]] = None,
                 data_kernel_prior: Optional[Callable[[], Dict[str, jnp.ndarray]]] = None,
                 mean_fn_prior: Optional[Callable[[], Dict[str, jnp.ndarray]]] = None,
                 noise_prior: Optional[Callable[[], Dict[str, jnp.ndarray]]] = None,
                 task_kernel_prior: Optional[Callable[[], Dict[str, jnp.ndarray]]] = None,
                 output_scale: bool = False, **kwargs) -> None:
        args = (input_dim, None, mean_fn, None, mean_fn_prior, noise_prior)
        super(MultiTaskGP, self).__init__(*args)
        if shared_input_space:
            if num_tasks is None:
                raise ValueError("Please specify num_tasks")
        else:
            if num_latents is None:
                raise ValueError("Please specify num_latents")
        self.num_tasks = num_tasks
        self.num_latents = num_tasks if num_latents is None else num_latents
        self.rank = rank
        self.kernel = LCMKernel(
            data_kernel, shared_input_space, num_tasks, **kwargs)
        self.data_kernel_name = data_kernel if isinstance(data_kernel, str) else None
        self.data_kernel_prior = data_kernel_prior
        self.task_kernel_prior = task_kernel_prior
        self.shared_input = shared_input_space
        self.output_scale = output_scale

    def model(self,
              X: jnp.ndarray,
              y: jnp.ndarray = None,
              **kwargs: float
              ) -> None:
        """Multitask GP probabilistic model with inputs X and targets y"""

        # Initialize mean function at zeros
        if self.shared_input:
            f_loc = jnp.zeros(self.num_tasks * X.shape[0])
        else:
            f_loc = jnp.zeros(X.shape[0])

        # Check that we have necessary info for sampling kernel params
        if not self.shared_input and self.num_tasks is None:
            self.num_tasks = len(onp.unique(self.X_train[:, -1]))

        if self.rank is None:
            self.rank = self.num_tasks - 1

        # Sample data kernel parameters
        if self.data_kernel_prior:
            data_kernel_params = self.data_kernel_prior()
        else:
            data_kernel_params = self._sample_kernel_params()

        # Sample task kernel parameters
        if self.task_kernel_prior:
            task_kernel_params = self.task_kernel_prior()
        else:
            task_kernel_params = self._sample_task_kernel_params()

        # Combine two dictionaries with parameters
        kernel_params = {**data_kernel_params, **task_kernel_params}

        # Sample noise
        if self.noise_prior:
            noise = self.noise_prior()
        else:
            noise = numpyro.sample(
                "noise", dist.LogNormal(
                    jnp.zeros(self.num_tasks),
                    jnp.ones(self.num_tasks)).to_event(1)
            )

        # Compute multitask_kernel
        k = self.kernel(X, X, kernel_params, noise, **kwargs)

        # Add mean function (if any)
        if self.mean_fn is not None:
            args = [X]
            if self.mean_fn_prior is not None:
                args += [self.mean_fn_prior()]
            f_loc += self.mean_fn(*args).squeeze()

        # Sample y according to the standard Gaussian process formula
        numpyro.sample(
            "y",
            dist.MultivariateNormal(loc=f_loc, covariance_matrix=k),
            obs=y,
        )

    def _sample_task_kernel_params(self):
        """
        Sample task kernel parameters with default weakly-informative priors
        for all the latent functions
        """
        W_dist = dist.Normal(
                jnp.zeros(shape=(self.num_latents, self.num_tasks, self.rank)),  # loc
                10*jnp.ones(shape=(self.num_latents, self.num_tasks, self.rank)) # var
        )
        v_dist = dist.LogNormal(
                jnp.zeros(shape=(self.num_latents, self.num_tasks)),  # loc
                jnp.ones(shape=(self.num_latents, self.num_tasks)) # var
        )
        with numpyro.plate("latent_plate_task", self.num_latents):
            W = numpyro.sample("W", W_dist.to_event(2))
            v = numpyro.sample("v", v_dist.to_event(1))
        return {"W": W, "v": v}

    def _sample_kernel_params(self):
        """
        Sample data ("base") kernel parameters with default weakly-informative
        priors for all the latent functions
        """
        squeezer = lambda x: x.squeeze() if self.num_latents > 1 else x
        with numpyro.plate("latent_plate_data", self.num_latents, dim=-2):
            with numpyro.plate("ard", self.kernel_dim, dim=-1):
                length = numpyro.sample("k_length", dist.LogNormal(0.0, 1.0))
            if self.output_scale:
                scale = numpyro.sample("k_scale", dist.LogNormal(0.0, 1.0))
            else:
                scale = numpyro.deterministic("k_scale", jnp.ones(self.num_latents))
            if self.data_kernel_name == 'Periodic':
                period = numpyro.sample("period", dist.LogNormal(0.0, 1.0))
        kernel_params = {
            "k_length": squeezer(length), "k_scale": squeezer(scale),
            "period": squeezer(period) if self.data_kernel_name == "Periodic" else None
        }
        return kernel_params
