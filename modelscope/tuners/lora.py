# Copyright (c) Alibaba, Inc. and its affiliates.
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License (MIT). See LICENSE in the repo root for license information.
import logging
import math
import os.path
import types
from typing import Dict, List

import torch
import torch.nn as nn
import torch.nn.functional as F

logger = logging.getLogger(__name__)


class LoRATuner:

    @staticmethod
    def tune(model: nn.Module,
             rank=6,
             replace_modules=None,
             lora_alpha=1.,
             lora_dropout=0.,
             merge_weights=True,
             fan_in_fan_out=False,
             bias='none',
             pretrained_tuner=None):
        """Tune a model with lora.

        Args:
            model: The torch.nn.Module containing the target module to be patched.
            rank: The lora rank.
            replace_modules: The module names to be replaced, the replacing strategy is `end with`.
            lora_alpha: The alpha value for lora module.
            lora_dropout: The dropout value for lora module.
            merge_weights: If merge_weights set to True, when the module turns to `eval`, the lora weights
                will be added into the origin weight to reduce calculation.
            fan_in_fan_out: Set this to True if the layer to replace stores weight like (fan_in, fan_out).
            bias: The grad strategy for bias, can be `none`, 'all' or 'lora_only'.
            pretrained_tuner: The pretrained file of lora.

        Returns:
            The lora modules
        """
        modules = LoRATuner._dynamic_patch_lora(
            model,
            replace_modules=replace_modules,
            r=rank,
            lora_alpha=lora_alpha,
            lora_dropout=lora_dropout,
            merge_weights=merge_weights,
            fan_in_fan_out=fan_in_fan_out)

        mark_only_lora_as_trainable(model, bias)

        def state_dict_hook(module, destination, prefix, local_metadata):
            return lora_state_dict(destination, bias)

        model.state_dict_hook_handle = model._register_state_dict_hook(
            state_dict_hook)

        def warning_hook(module, incompatible_keys):
            logger.info(
                f'The {module.__class__.__name__} module has unmatched keys: {incompatible_keys},'
                f'this is converted to a notice with respect to LoRA')
            for ik in incompatible_keys:
                ik.clear()

        if hasattr(model, 'register_load_state_dict_post_hook'):
            model.load_state_dict_hook_handle = model.register_load_state_dict_post_hook(
                warning_hook)
        else:

            def load_state_dict(self, state_dict, strict=True):
                return self.load_state_dict_origin(state_dict, False)

            model.load_state_dict_origin = model.load_state_dict
            model.load_state_dict = types.MethodType(load_state_dict, model)

        if pretrained_tuner is not None and os.path.isfile(pretrained_tuner):
            logger.info(f'Loading LoRA weights from file: {pretrained_tuner}')
            model.load_state_dict(torch.load(pretrained_tuner))

        return modules

    @staticmethod
    def _dynamic_patch_lora(model, replace_modules, **kwargs):
        """Dynamic patch lora to model

        Args:
            model: The torch.nn.Module containing the target module to be patched.
            replace_modules: The module names to be replaced, the replacing strategy is `end with`.
            **kwargs: The arguments passed from `tune` which are needed by lora.

        Returns:
            The lora modules
        """
        modules = []
        module_keys = [key for key, _ in model.named_modules()]
        assert isinstance(replace_modules, (str, list))
        if isinstance(replace_modules, str):
            replace_modules = [replace_modules]

        for module_key in module_keys:
            if any([module_key.endswith(name)
                    for name in replace_modules]):  # noqa
                parts = module_key.split('.')
                module = model.get_submodule('.'.join(parts[:-1]))
                sub_module = model.get_submodule(module_key)
                _key = parts[-1]

                lora_module = None
                if isinstance(sub_module, torch.nn.Linear):
                    lora_module = Linear(
                        sub_module.in_features,
                        sub_module.out_features,
                        bias=sub_module.bias is not None,
                        **kwargs)
                elif isinstance(sub_module, torch.nn.Conv2d):
                    kwargs.pop('fan_in_fan_out', None)
                    lora_module = Conv2d(
                        sub_module.in_channels,
                        sub_module.out_channels,
                        kernel_size=sub_module.kernel_size,
                        stride=sub_module.stride,
                        padding=sub_module.padding,
                        dilation=sub_module.dilation,
                        groups=sub_module.groups,
                        **kwargs)

                if lora_module is not None:
                    lora_module.weight = sub_module.weight
                    if sub_module.bias is not None:
                        lora_module.bias = sub_module.bias
                    lora_module.to(sub_module.weight.device).to(
                        sub_module.weight.dtype)
                    setattr(module, _key, lora_module)
                    modules.append(lora_module)
        return modules

    @staticmethod
    def unpatch_lora(model, replace_modules):
        """Unpatch lora modules and merge the weights to original modules.

        Args:
            model: The model called with `tune` function.
            replace_modules: The module names to be replaced, the replacing strategy is `end with`.

        Returns:
            The lora modules.
        """
        modules = []
        module_keys = [key for key, _ in model.named_modules()]
        assert isinstance(replace_modules, (str, list))
        if isinstance(replace_modules, str):
            replace_modules = [replace_modules]

        for module_key in module_keys:
            if any([module_key.endswith(name)
                    for name in replace_modules]):  # noqa
                parts = module_key.split('.')
                module = model.get_submodule('.'.join(parts[:-1]))
                sub_module = model.get_submodule(module_key)
                _key = parts[-1]

                origin_module = None
                if isinstance(sub_module, Linear):
                    origin_module = torch.nn.Linear(
                        sub_module.in_features,
                        sub_module.out_features,
                        bias=sub_module.bias is not None)
                elif isinstance(sub_module, Conv2d):
                    origin_module = torch.nn.Conv2d(
                        sub_module.in_channels,
                        sub_module.out_channels,
                        kernel_size=sub_module.kernel_size,
                        stride=sub_module.stride,
                        padding=sub_module.padding,
                        dilation=sub_module.dilation,
                        groups=sub_module.groups)

                if origin_module is not None:
                    sub_module.merge_weights = True
                    sub_module.eval()
                    origin_module.weight = sub_module.weight
                    if sub_module.bias is not None:
                        origin_module.bias = sub_module.bias
                    origin_module.to(sub_module.weight.device).to(
                        sub_module.weight.dtype)
                    setattr(module, _key, origin_module)
                    modules.append(sub_module)

        model.state_dict_hook_handle.remove()
        if hasattr(model, 'load_state_dict_hook_handle'):
            model.load_state_dict_hook_handle.remove()
        else:
            model.load_state_dict = model.load_state_dict_origin
        return modules


class LoRALayer:

    def __init__(
        self,
        r: int,
        lora_alpha: int,
        lora_dropout: float,
        merge_weights: bool,
    ):
        self.r = r
        self.lora_alpha = lora_alpha
        # Optional dropout
        if lora_dropout > 0.:
            self.lora_dropout = nn.Dropout(p=lora_dropout)
        else:
            self.lora_dropout = lambda x: x
        # Mark the weight as unmerged
        self.merged = False
        self.merge_weights = merge_weights


class Embedding(nn.Embedding, LoRALayer):
    # LoRA implemented in a dense layer
    def __init__(self,
                 num_embeddings: int,
                 embedding_dim: int,
                 r: int = 0,
                 lora_alpha: int = 1,
                 merge_weights: bool = True,
                 **kwargs):
        nn.Embedding.__init__(self, num_embeddings, embedding_dim, **kwargs)
        LoRALayer.__init__(
            self,
            r=r,
            lora_alpha=lora_alpha,
            lora_dropout=0,
            merge_weights=merge_weights)
        # Actual trainable parameters
        if r > 0:
            self.lora_A = nn.Parameter(
                self.weight.new_zeros((r, num_embeddings)))
            self.lora_B = nn.Parameter(
                self.weight.new_zeros((embedding_dim, r)))
            self.scaling = self.lora_alpha / self.r
            # Freezing the pre-trained weight matrix
            self.weight.requires_grad = False
        self.reset_parameters()

    def reset_parameters(self):
        nn.Embedding.reset_parameters(self)
        if hasattr(self, 'lora_A'):
            # initialize A the same way as the default for nn.Linear and B to zero
            nn.init.zeros_(self.lora_A)
            nn.init.normal_(self.lora_B)

    def train(self, mode: bool = True):
        nn.Embedding.train(self, mode)
        self.lora_A.requires_grad = mode
        self.lora_B.requires_grad = mode
        if mode and self.merge_weights and self.merged:
            # Make sure that the weights are not merged
            if self.r > 0:
                self.weight.data -= (self.lora_B
                                     @ self.lora_A).T * self.scaling
            self.merged = False
        if not mode and self.merge_weights and not self.merged:
            # Merge the weights and mark it
            if self.r > 0:
                self.weight.data += (self.lora_B
                                     @ self.lora_A).T * self.scaling
            self.merged = True

    def eval(self):
        nn.Embedding.eval(self)
        self.lora_A.requires_grad = False
        self.lora_B.requires_grad = False
        if self.merge_weights and not self.merged:
            # Merge the weights and mark it
            if self.r > 0:
                self.weight.data += (self.lora_B @ self.lora_A) * self.scaling
            self.merged = True

    def forward(self, x: torch.Tensor):
        if self.r > 0 and not self.merged:
            result = nn.Embedding.forward(self, x)
            if self.r > 0:
                after_A = F.embedding(x, self.lora_A.T, self.padding_idx,
                                      self.max_norm, self.norm_type,
                                      self.scale_grad_by_freq, self.sparse)
                result += (after_A @ self.lora_B.T) * self.scaling
            return result
        else:
            return nn.Embedding.forward(self, x)


class Linear(nn.Linear, LoRALayer):
    # LoRA implemented in a dense layer
    def __init__(
            self,
            in_features: int,
            out_features: int,
            r: int = 0,
            lora_alpha: int = 1,
            lora_dropout: float = 0.,
            fan_in_fan_out: bool = False,
            # Set this to True if the layer to replace stores weight like (fan_in, fan_out)
            merge_weights: bool = True,
            **kwargs):
        nn.Linear.__init__(self, in_features, out_features, **kwargs)
        LoRALayer.__init__(
            self,
            r=r,
            lora_alpha=lora_alpha,
            lora_dropout=lora_dropout,
            merge_weights=merge_weights)

        self.fan_in_fan_out = fan_in_fan_out
        # Actual trainable parameters
        if r > 0:
            self.lora_A = nn.Parameter(self.weight.new_zeros((r, in_features)))
            self.lora_B = nn.Parameter(
                self.weight.new_zeros((out_features, r)))
            self.scaling = self.lora_alpha / self.r
            # Freezing the pre-trained weight matrix
            self.weight.requires_grad = False
        self.reset_parameters()
        if fan_in_fan_out:
            self.weight.data = self.weight.data.T

    def reset_parameters(self):
        nn.Linear.reset_parameters(self)
        if hasattr(self, 'lora_A'):
            # initialize A the same way as the default for nn.Linear and B to zero
            nn.init.kaiming_uniform_(self.lora_A, a=math.sqrt(5))
            nn.init.zeros_(self.lora_B)

    def train(self, mode: bool = True):

        def T(w):
            return w.T if self.fan_in_fan_out else w

        nn.Linear.train(self, mode)
        self.lora_A.requires_grad = mode
        self.lora_B.requires_grad = mode
        if mode and self.merge_weights and self.merged:
            # Make sure that the weights are not merged
            if self.r > 0:
                self.weight.data -= T(self.lora_B @ self.lora_A) * self.scaling
            self.merged = False
        if not mode and self.merge_weights and not self.merged:
            # Merge the weights and mark it
            if self.r > 0:
                self.weight.data += T(self.lora_B @ self.lora_A) * self.scaling
            self.merged = True

    def eval(self):

        def T(w):
            return w.T if self.fan_in_fan_out else w

        nn.Linear.eval(self)
        self.lora_A.requires_grad = False
        self.lora_B.requires_grad = False
        if self.merge_weights and not self.merged:
            # Merge the weights and mark it
            if self.r > 0:
                self.weight.data += T(self.lora_B @ self.lora_A) * self.scaling
            self.merged = True

    def forward(self, x: torch.Tensor):

        def T(w):
            return w.T if self.fan_in_fan_out else w

        if self.r > 0 and not self.merged:
            result = F.linear(x, T(self.weight), bias=self.bias)
            if self.r > 0:
                result += (self.lora_dropout(x) @ self.lora_A.T
                           @ self.lora_B.T) * self.scaling
            return result
        else:
            return F.linear(x, T(self.weight), bias=self.bias)


class MergedLinear(nn.Linear, LoRALayer):
    # LoRA implemented in a dense layer
    def __init__(self,
                 in_features: int,
                 out_features: int,
                 r: int = 0,
                 lora_alpha: int = 1,
                 lora_dropout: float = 0.,
                 enable_lora: List[bool] = [False],
                 fan_in_fan_out: bool = False,
                 merge_weights: bool = True,
                 **kwargs):
        nn.Linear.__init__(self, in_features, out_features, **kwargs)
        LoRALayer.__init__(
            self,
            r=r,
            lora_alpha=lora_alpha,
            lora_dropout=lora_dropout,
            merge_weights=merge_weights)
        assert out_features % len(enable_lora) == 0, \
            'The length of enable_lora must divide out_features'
        self.enable_lora = enable_lora
        self.fan_in_fan_out = fan_in_fan_out
        # Actual trainable parameters
        if r > 0 and any(enable_lora):
            self.lora_A = nn.Parameter(
                self.weight.new_zeros((r * sum(enable_lora), in_features)))
            self.lora_B = nn.Parameter(
                self.weight.new_zeros(
                    (out_features // len(enable_lora) * sum(enable_lora),
                     r)))  # weights for Conv1D with groups=sum(enable_lora)
            self.scaling = self.lora_alpha / self.r
            # Freezing the pre-trained weight matrix
            self.weight.requires_grad = False
            # Compute the indices
            self.lora_ind = self.weight.new_zeros(
                (out_features, ), dtype=torch.bool).view(len(enable_lora), -1)
            self.lora_ind[enable_lora, :] = True
            self.lora_ind = self.lora_ind.view(-1)
        self.reset_parameters()
        if fan_in_fan_out:
            self.weight.data = self.weight.data.T

    def reset_parameters(self):
        nn.Linear.reset_parameters(self)
        if hasattr(self, 'lora_A'):
            # initialize A the same way as the default for nn.Linear and B to zero
            nn.init.kaiming_uniform_(self.lora_A, a=math.sqrt(5))
            nn.init.zeros_(self.lora_B)

    def zero_pad(self, x):
        result = x.new_zeros((*x.shape[:-1], self.out_features))
        result = result.view(-1, self.out_features)
        result[:, self.lora_ind] = x.reshape(
            -1,
            self.out_features // len(self.enable_lora) * sum(self.enable_lora))
        return result.view((*x.shape[:-1], self.out_features))

    def train(self, mode: bool = True):

        def T(w):
            return w.T if self.fan_in_fan_out else w

        nn.Linear.train(self, mode)
        self.lora_A.requires_grad = mode
        self.lora_B.requires_grad = mode
        if mode and self.merge_weights and self.merged:
            # Make sure that the weights are not merged
            if self.r > 0 and any(self.enable_lora):
                delta_w = F.conv1d(
                    self.lora_A.data.unsqueeze(0),
                    self.lora_B.data.unsqueeze(-1),
                    groups=sum(self.enable_lora)).squeeze(0)
                self.weight.data -= self.zero_pad(T(delta_w * self.scaling))
            self.merged = False
        if not mode and self.merge_weights and not self.merged:
            if self.r > 0 and any(self.enable_lora):
                delta_w = F.conv1d(
                    self.lora_A.data.unsqueeze(0),
                    self.lora_B.data.unsqueeze(-1),
                    groups=sum(self.enable_lora)).squeeze(0)
                self.weight.data += self.zero_pad(T(delta_w * self.scaling))
            self.merged = True

    def eval(self):

        def T(w):
            return w.T if self.fan_in_fan_out else w

        nn.Linear.eval(self)
        self.lora_A.requires_grad = False
        self.lora_B.requires_grad = False
        if self.merge_weights and not self.merged:
            # Merge the weights and mark it
            if self.r > 0 and any(self.enable_lora):
                delta_w = F.conv1d(
                    self.lora_A.data.unsqueeze(0),
                    self.lora_B.data.unsqueeze(-1),
                    groups=sum(self.enable_lora)).squeeze(0)
                self.weight.data += self.zero_pad(T(delta_w * self.scaling))
            self.merged = True

    def forward(self, x: torch.Tensor):

        def T(w):
            return w.T if self.fan_in_fan_out else w

        if self.merged:
            return F.linear(x, T(self.weight), bias=self.bias)
        else:
            result = F.linear(x, T(self.weight), bias=self.bias)
            if self.r > 0:
                after_A = F.linear(self.lora_dropout(x), self.lora_A)
                after_B = F.conv1d(
                    after_A.transpose(-2, -1),
                    self.lora_B.unsqueeze(-1),
                    groups=sum(self.enable_lora)).transpose(-2, -1)
                result += self.zero_pad(after_B) * self.scaling
            return result


class Conv2d(nn.Conv2d, LoRALayer):
    # LoRA implemented in a dense layer
    def __init__(self,
                 in_channels: int,
                 out_channels: int,
                 kernel_size: int,
                 r: int = 0,
                 lora_alpha: int = 1,
                 lora_dropout: float = 0.,
                 merge_weights: bool = True,
                 **kwargs):
        nn.Conv2d.__init__(self, in_channels, out_channels, kernel_size,
                           **kwargs)
        LoRALayer.__init__(
            self,
            r=r,
            lora_alpha=lora_alpha,
            lora_dropout=lora_dropout,
            merge_weights=merge_weights)
        assert type(kernel_size) is int
        # Actual trainable parameters
        if r > 0:
            self.lora_A = nn.Parameter(
                self.weight.new_zeros(
                    (r * kernel_size, in_channels * kernel_size)))
            self.lora_B = nn.Parameter(
                self.weight.new_zeros(
                    (out_channels * kernel_size, r * kernel_size)))
            self.scaling = self.lora_alpha / self.r
            # Freezing the pre-trained weight matrix
            self.weight.requires_grad = False
        self.reset_parameters()

    def reset_parameters(self):
        nn.Conv2d.reset_parameters(self)
        if hasattr(self, 'lora_A'):
            # initialize A the same way as the default for nn.Linear and B to zero
            nn.init.kaiming_uniform_(self.lora_A, a=math.sqrt(5))
            nn.init.zeros_(self.lora_B)

    def train(self, mode: bool = True):
        nn.Conv2d.train(self, mode)
        self.lora_A.requires_grad = mode
        self.lora_B.requires_grad = mode
        if mode and self.merge_weights and self.merged:
            # Make sure that the weights are not merged
            self.weight.data -= (self.lora_B @ self.lora_A).view(
                self.weight.shape) * self.scaling
            self.merged = False
        if not mode and self.merge_weights and not self.merged:
            self.weight.data += (self.lora_B @ self.lora_A).view(
                self.weight.shape) * self.scaling
            self.merged = True

    def eval(self):
        nn.Conv2d.eval(self)
        self.lora_A.requires_grad = False
        self.lora_B.requires_grad = False
        if self.merge_weights and not self.merged:
            # Merge the weights and mark it
            self.weight.data += (self.lora_B @ self.lora_A).view(
                self.weight.shape) * self.scaling
            self.merged = True

    def forward(self, x: torch.Tensor):
        if self.r > 0 and not self.merged:
            return F.conv2d(
                x,
                self.weight +  # noqa
                (self.lora_B @ self.lora_A).view(self.weight.shape)  # noqa
                * self.scaling,
                self.bias,
                self.stride,
                self.padding,
                self.dilation,
                self.groups)
        return nn.Conv2d.forward(self, x)


def mark_only_lora_as_trainable(model: nn.Module, bias: str = 'none') -> None:
    for n, p in model.named_parameters():
        if 'lora_' not in n:
            p.requires_grad = False
    if bias == 'none':
        return
    elif bias == 'all':
        for n, p in model.named_parameters():
            if 'bias' in n:
                p.requires_grad = True
    elif bias == 'lora_only':
        for m in model.modules():
            if isinstance(m, LoRALayer) and \
                    hasattr(m, 'bias') and \
                    m.bias is not None:
                m.bias.requires_grad = True
    else:
        raise NotImplementedError


def lora_state_dict(state_dict, bias: str = 'none') -> Dict[str, torch.Tensor]:
    if bias == 'none':
        return {k: state_dict[k] for k in state_dict if 'lora_' in k}
    elif bias == 'all':
        return {
            k: state_dict[k]
            for k in state_dict if 'lora_' in k or 'bias' in k
        }
    elif bias == 'lora_only':
        to_return = {}
        for k in state_dict:
            if 'lora_' in k:
                to_return[k] = state_dict[k]
                bias_name = k.split('lora_')[0] + 'bias'
                if bias_name in state_dict:
                    to_return[bias_name] = state_dict[bias_name]
        return to_return
    else:
        raise NotImplementedError
