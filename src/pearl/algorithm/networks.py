"""
Various network architecture implementations used in PEARL algorithm
"""

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Normal


class MLP(nn.Module):
    """Base MLP network class"""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        input_dim,
        output_dim,
        hidden_dim,
        hidden_activation=F.relu,
        init_w=3e-3,
    ):
        super().__init__()

        self.input_dim = input_dim
        self.output_dim = output_dim
        self.hidden_activation = hidden_activation

        # Set fully connected layers
        self.fc_layers = nn.ModuleList()
        self.hidden_layers = [hidden_dim] * 3
        in_layer = input_dim

        for i, hidden_layer in enumerate(self.hidden_layers):
            fc_layer = nn.Linear(in_layer, hidden_layer)
            in_layer = hidden_layer
            self.__setattr__("fc_layer{}".format(i), fc_layer)
            self.fc_layers.append(fc_layer)

        # Set the output layer
        self.last_fc_layer = nn.Linear(hidden_dim, output_dim)
        self.last_fc_layer.weight.data.uniform_(-init_w, init_w)
        self.last_fc_layer.bias.data.uniform_(-init_w, init_w)

    def forward(self, x):
        """Get output when input is given"""
        for fc_layer in self.fc_layers:
            x = self.hidden_activation(fc_layer(x))
        x = self.last_fc_layer(x)
        return x


class FlattenMLP(MLP):
    """
    Flatten MLP network class
    If there are multiple inputs, concatenate along dim -1
    """

    def forward(self, *x):
        x = torch.cat(x, dim=-1)
        return super().forward(x)


class MLPEncoder(FlattenMLP):
    """
    Context encoder network class
    that contain various compututation for context variable z
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        input_dim,
        output_dim,
        latent_dim,
        hidden_dim,
        device,
    ):
        super().__init__(input_dim=input_dim, output_dim=output_dim, hidden_dim=hidden_dim)

        self.output_dim = output_dim
        self.latent_dim = latent_dim
        self.device = device

        self.z_mean = None
        self.z_var = None
        self.task_z = None
        self.clear_z()

    def clear_z(self, num_tasks=1):
        """
        Reset q(z|c) to the prior r(z)
        Sample a new z from the prior r(z)
        Reset the context collected so far
        """
        # Reset q(z|c) to the prior r(z)
        self.z_mean = torch.zeros(num_tasks, self.latent_dim).to(self.device)
        self.z_var = torch.ones(num_tasks, self.latent_dim).to(self.device)

        # Sample a new z from the prior r(z)
        self.sample_z()

        # Reset the context collected so far
        self.context = None

    def sample_z(self):
        """Sample z ~ r(z) or z ~ q(z|c)"""
        dists = []
        for mean, var in zip(torch.unbind(self.z_mean), torch.unbind(self.z_var)):
            dist = torch.distributions.Normal(mean, torch.sqrt(var))
            dists.append(dist)
        sampled_z = [dist.rsample() for dist in dists]
        self.task_z = torch.stack(sampled_z).to(self.device)

    @classmethod
    def product_of_gaussians(cls, mean, var):
        """Compute mean, stddev of product of gaussians (POG)"""
        var = torch.clamp(var, min=1e-7)
        pog_var = 1.0 / torch.sum(torch.reciprocal(var), dim=0)
        pog_mean = pog_var * torch.sum(mean / var, dim=0)
        return pog_mean, pog_var

    def infer_posterior(self, context):
        """Compute q(z|c) as a function of input context and sample new z from it"""
        params = self.forward(context)
        params = params.view(context.size(0), -1, self.output_dim).to(self.device)

        # With probabilistic z, predict mean and variance of q(z | c)
        z_mean = torch.unbind(params[..., : self.latent_dim])
        z_var = torch.unbind(F.softplus(params[..., self.latent_dim :]))
        z_params = [self.product_of_gaussians(mu, var) for mu, var in zip(z_mean, z_var)]

        self.z_mean = torch.stack([z_param[0] for z_param in z_params]).to(self.device)
        self.z_var = torch.stack([z_param[1] for z_param in z_params]).to(self.device)
        self.sample_z()

    def compute_kl_div(self):
        """Compute KL( q(z|c) || r(z) )"""
        prior = torch.distributions.Normal(
            torch.zeros(self.latent_dim).to(self.device),
            torch.ones(self.latent_dim).to(self.device),
        )

        posteriors = []
        for mean, var in zip(torch.unbind(self.z_mean), torch.unbind(self.z_var)):
            dist = torch.distributions.Normal(mean, torch.sqrt(var))
            posteriors.append(dist)

        kl_div = [torch.distributions.kl.kl_divergence(posterior, prior) for posterior in posteriors]
        kl_div = torch.stack(kl_div).sum().to(self.device)
        return kl_div


LOG_SIG_MAX = 2
LOG_SIG_MIN = -20


class TanhGaussianPolicy(MLP):
    """Gaussian policy network class using MLP and tanh activation function"""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        input_dim,
        output_dim,
        hidden_dim,
        is_deterministic=False,
        init_w=1e-3,
    ):
        super().__init__(
            input_dim=input_dim,
            output_dim=output_dim,
            hidden_dim=hidden_dim,
            init_w=init_w,
        )

        self.is_deterministic = is_deterministic
        self.last_fc_log_std = nn.Linear(hidden_dim, output_dim)
        self.last_fc_log_std.weight.data.uniform_(-init_w, init_w)
        self.last_fc_log_std.bias.data.uniform_(-init_w, init_w)

    def forward(self, x):
        for fc_layer in self.fc_layers:
            x = self.hidden_activation(fc_layer(x))

        mean = self.last_fc_layer(x)
        log_std = self.last_fc_log_std(x)
        log_std = torch.clamp(log_std, LOG_SIG_MIN, LOG_SIG_MAX)
        std = torch.exp(log_std)

        if self.is_deterministic:
            action = torch.tanh(mean)
            log_prob = None
        else:
            normal = Normal(mean, std)
            # If reparameterize, use reparameterization trick (mean + std * N(0,1))
            action = normal.rsample()

            # Compute log prob from Gaussian,
            # and then apply correction for Tanh squashing.
            # NOTE: The correction formula is a little bit magic.
            # To get an understanding of where it comes from,
            # check out the original SAC paper
            # (https://arxiv.org/abs/1801.01290) and look in appendix C.
            # This is a more numerically-stable equivalent to Eq 21.
            # Derivation:
            #               log(1 - tanh(x)^2))
            #               = log(sech(x)^2))
            #               = 2 * log(sech(x)))
            #               = 2 * log(2e^-x / (e^-2x + 1)))
            #               = 2 * (log(2) - x - log(e^-2x + 1)))
            #               = 2 * (log(2) - x - softplus(-2x)))
            log_prob = normal.log_prob(action)
            log_prob -= 2 * (np.log(2) - action - F.softplus(-2 * action))
            log_prob = log_prob.sum(-1, keepdim=True)

        action = torch.tanh(action)
        return action, log_prob