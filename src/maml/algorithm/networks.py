"""
Various network architecture codes used in MAML algorithm
"""

from typing import Any, Tuple

import torch
import torch.nn as nn
from torch.distributions import Normal


class MLP(nn.Module):
    """Base MLP network class"""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        input_dim: int,
        output_dim: int,
        hidden_dim: int,
        hidden_activation: Any = torch.tanh,
    ) -> None:
        super().__init__()

        self.input_dim = input_dim
        self.output_dim = output_dim
        self.hidden_activation = hidden_activation

        # Set fully connected layers
        self.fc_layers = nn.ModuleList()
        self.hidden_layers = [hidden_dim] * 2
        in_layer = input_dim

        for i, hidden_layer in enumerate(self.hidden_layers):
            fc_layer = nn.Linear(in_layer, hidden_layer)
            nn.init.xavier_uniform_(fc_layer.weight.data)
            fc_layer.bias.data.zero_()
            in_layer = hidden_layer
            self.__setattr__("fc_layer{}".format(i), fc_layer)
            self.fc_layers.append(fc_layer)

        # Set the output layer
        self.last_fc_layer = nn.Linear(hidden_dim, output_dim)
        nn.init.xavier_uniform_(self.last_fc_layer.weight.data)
        self.last_fc_layer.bias.data.zero_()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Get output when input is given"""
        for fc_layer in self.fc_layers:
            x = self.hidden_activation(fc_layer(x))
        x = self.last_fc_layer(x)
        return x


class GaussianPolicy(MLP):
    """Gaussian policy network class"""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        input_dim: int,
        output_dim: int,
        hidden_dim: int,
        is_deterministic: bool = False,
        init_std: float = 1.0,
        min_std: float = 1e-6,
        max_std: float = None,
    ) -> None:
        super().__init__(
            input_dim=input_dim,
            output_dim=output_dim,
            hidden_dim=hidden_dim,
        )

        self.log_std = torch.Tensor([init_std]).log()
        self.log_std = torch.nn.Parameter(self.log_std)
        self.min_log_std = None
        self.max_log_std = None

        if min_std is not None:
            self.min_log_std = torch.Tensor([min_std]).log()
        if max_std is not None:
            self.max_log_std = torch.Tensor([max_std]).log()

        self.is_deterministic = is_deterministic

    def get_normal_dist(self, x: torch.Tensor) -> Tuple[Normal, torch.Tensor]:
        """Get Gaussian distribtion"""
        mean = super().forward(x)
        std = torch.exp(self.log_std.clamp(min=self.min_log_std, max=self.max_log_std))

        return Normal(mean, std), mean

    def get_log_prob(self, obs: torch.Tensor, action: torch.Tensor) -> torch.Tensor:
        """Get log probability of Gaussian distribution using obs and action"""
        normal, _ = self.get_normal_dist(obs)

        return normal.log_prob(action).sum(dim=-1)

    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, ...]:
        normal, mean = self.get_normal_dist(x)
        if self.is_deterministic:
            action = mean
            log_prob = torch.zeros(1)
        else:
            action = normal.sample()
            log_prob = normal.log_prob(action).sum(dim=-1)
        action = action.view(-1)

        return action, log_prob
