import types
import logging
import inspect

import torch
import torch.nn as nn
import torch.nn.init as init

from . import utils


def unsqueeze_expand_dim(x, dim, k):
    dims = x.size()
    return x.unsqueeze(dim).expand(*dims[:dim], k, *dims[dim:])


def expand_match(a, b, dim=0):
    size = [-1] * len(b.size())
    size[dim] = b.size(dim)
    return a.unsqueeze(dim).expand(*size)


def resolve_cls(name, module):
    if inspect.isclass(name):
        return name
    else:
        return utils.resolve_obj(module, name)


def recursively_reset_parameters(parent):
    for module in parent.children():
        if hasattr(module, "reset_parameters"):
            module.reset_parameters()


class Sequential(nn.Sequential):

    def reset_parameters(self):
        recursively_reset_parameters(self)


class ModuleList(nn.ModuleList):

    def reset_parameters(self):
        recursively_reset_parameters(self)


class Linear(nn.Linear):

    def reset_parameters(self):
        init.xavier_normal_(self.weight.detach())

        if self.bias is not None:
            self.bias.detach().zero_()


class ArgumentDictionary(dict):

    @staticmethod
    def from_namespace(namespace):
        return ArgumentDictionary(vars(namespace))

    def filter(self, key):
        return ArgumentDictionary({k[len(key) + 1:]: v for k, v in self.items()
                                   if k.startswith(key) and k != key})


class InitializerCreator(object):

    def __init__(self, pkg):
        self.pkg = pkg
        self.cls = None
        self.kwargs = dict()

    def initialize(self, *args, **kwargs):
        assert self.cls is not None
        return self.cls(*args, **kwargs, **self.kwargs)

    def __call__(self, argdict):
        if isinstance(argdict, str):
            name, kwargs = argdict, {}
        else:
            name, kwargs = argdict.get("type"), argdict.get("vargs", {})
        parent_modname = ".".join(__name__.split(".")[:-1])
        manager_modname = f"{parent_modname}.manager"
        manager = utils.import_module(manager_modname)
        self.cls = manager.resolve(name, self.pkg)
        assert self.cls is not None, \
            f"module type not found in {self.pkg.__name__}: {name}"
        self.kwargs = self.cls.process_argdict(kwargs)
        return self.initialize


def is_module_cls(x):
    return inspect.isclass(x) and issubclass(x, Module)


def get_caster(value):
    if is_module_cls(value):
        cls = value
        return InitializerCreator(cls.get_package())
    else:
        return type(value)


class OptionalArgument(object):

    def __init__(self, name, caster=str, default=None, islist=False):
        assert caster is not None
        self.name = name
        self.caster = caster
        self.islist = islist
        self.default = default

    def __call__(self, value=None):
        """consumes any acceptable form of value and returns typed value compatible
        with the argument"""
        if value is None:
            return self.default
        if self.islist:
            if not isinstance(value, (list, tuple)):
                values = [value]
            else:
                values = value
            return [self.caster(value) for value in values]
        else:
            return self.caster(value)


def nullable_add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a + b


class Module(nn.Module):
    """The way you define your custom module is quite elegant.
    The common painstaking part of building a custom module is that
    module arguments could mean anything.
    Using this elegant framework, positional arguments are only reserved for
    model dimensions: arguments that affect the dimensions of input and output
    tensors; while optional arguments are used as augmenting parameters that
    could modify the behavior of the module."""
    name = None

    def __init__(self, *args, **kwargs):
        super(Module, self).__init__()
        if args or kwargs:
            logging.warning(f"module '{self.__class__}' received unexpected "
                            f"keyword arguments: {(args, kwargs)}")
        self.loss = None

    @classmethod
    def get_package(cls):
        pkg_name = cls.__module__
        return utils.import_module(pkg_name)

    @classmethod
    def get_optargdefaults(cls, parents=True):
        optargs = cls.get_optargs(parents)
        return {name: optarg.default for name, optarg in optargs.items()}

    @classmethod
    def get_optargs(cls, parents=True):
        """module optional arguments"""
        if cls == Module:
            return dict()
        optargs = {}
        kwonly = inspect.getfullargspec(cls.__init__).kwonlydefaults
        kwonly = dict() if kwonly is None else kwonly
        for name, default in kwonly.items():
            islist = False
            caster = get_caster(default)
            if isinstance(default, (list, tuple)):
                assert len(default) > 0, \
                    "list arguments must contain at least one element"
                caster = get_caster(default[0])
                islist = True
            arg = OptionalArgument(
                name=name,
                caster=caster,
                default=default,
                islist=islist
            )
            optargs[arg.name] = arg
        if parents:
            for base in cls.__bases__:
                if not issubclass(base, Module):
                    continue
                optargs.update(base.get_optargs(True))
        return optargs

    @classmethod
    def process_argdict(cls, argdict: dict):
        optargs = cls.get_optargs()
        return {
            name: optarg(argdict.get(name))
            for name, optarg in optargs.items()
        }

    def reset_parameters(self):
        recursively_reset_parameters(self)


class AmbidextrousModule(Module):
    """Ambidextrous Module for Side-tracking Losses

    This extension allows modules to create losses in addition to the standard
    forward-returns. All modules will automatically cumulate loss items
    from their own children and return the total loss to their calling modules.
    Ordinary modules cannot call AmbidextrousModule but the reverse is possible.
    Implement `forward_multi` instead.
    """
    def invoke(self, module, *args):
        ret = module(*args)
        if isinstance(ret, dict):
            loss = ret.get("loss")
            if loss is not None:
                if self.loss is not None:
                    self.loss += loss
                else:
                    self.loss = loss
            ret = ret.get("pass")
        return ret

    def forward(self, *input):
        # yield "loss", 0
        # yield "pass", 0
        # return <pass>
        raise NotImplementedError()

    def forward_multi(self, *input):
        self.loss = None
        ret = self.forward(*input)
        if isinstance(ret, types.GeneratorType):
            ret = dict(ret)
            pss = ret.get("pass")
            loss = ret.get("loss")
        else:
            pss = ret
            loss = None
        if torch.is_grad_enabled() and \
                (loss is not None or self.loss is not None):
            return {
                "pass": pss,
                "loss": nullable_add(loss, self.loss)
            }
        else:
            return {"pass": pss}


class Identity(Module):

    def __init__(self):
        super(Identity, self).__init__()

    def forward(self, x):
        return x


class Concat(Module):

    def __init__(self, dim=1):
        super(Concat, self).__init__()
        self.dim = dim

    def forward(self, *xs):
        return torch.cat(xs, self.dim)


class Parameter(nn.Parameter):

    def reset_parameters(self):
        self.data.detach().zero_()
