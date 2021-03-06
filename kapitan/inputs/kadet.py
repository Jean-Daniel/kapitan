#!/usr/bin/env python3

# Copyright 2019 The Kapitan Authors
# SPDX-FileCopyrightText: 2020 The Kapitan Authors <kapitan-admins@googlegroups.com>
#
# SPDX-License-Identifier: Apache-2.0
import glob
import inspect
import itertools
import json
import logging
import os
import sys
import yaml
from addict import Dict
from functools import cached_property
from importlib.abc import MetaPathFinder
from importlib.machinery import PathFinder
from importlib.util import module_from_spec, spec_from_file_location
from kapitan.errors import CompileError
from kapitan.inputs.base import CompiledFile, InputType
from kapitan.resources import inventory as inventory_func
from kapitan.utils import prune_empty
from typing import Collection

logger = logging.getLogger(__name__)
inventory = None
inventory_global = None
search_paths = None


def module_from_path(path, check_name=None):
    """
    loads python module in path
    set check_name to verify module name against spec name
    returns tuple with module and spec
    """

    if os.path.isdir(path):
        module_name = os.path.basename(os.path.normpath(path))
        init_path = os.path.join(path, "__init__.py")
    else:
        init_path = os.path.normpath(path)
        module_name, _ = os.path.splitext(os.path.basename(init_path))

    spec = spec_from_file_location("kadet_component_{}".format(module_name), init_path)

    if spec is None:
        raise ModuleNotFoundError("Could not load module in path {}".format(path))
    if check_name is not None and check_name != module_name:
        raise ModuleNotFoundError(
            "Module name {} does not match check_name {}".format(module_name, check_name)
        )

    mod = module_from_spec(spec)
    return mod, spec


def _load_from_search_paths(module_name, search_paths):
    """
    loads and executes python module with module_name from search paths
    returns module
    """
    for path in search_paths:
        try:
            _path = os.path.join(path, module_name)
            mod, spec = module_from_path(_path, check_name=module_name)
        except (ModuleNotFoundError, FileNotFoundError):
            continue
        # register module -> required to perform relative import in imported module
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod
    raise ModuleNotFoundError("Could not load module name {}".format(module_name))


class KadetFinder(MetaPathFinder):
    def __init__(self, search_paths):
        self.search_paths = search_paths

    def find_spec(self, fullname: str, path=None, target=None):
        return PathFinder.find_spec(fullname, path or self.search_paths, target)


def load_from_search_paths(module_name):
    return _load_from_search_paths(module_name, search_paths)


class Kadet(InputType):
    def __init__(self, compile_path, search_paths, ref_controller):
        super().__init__("kadet", compile_path, search_paths, ref_controller)
        self.input_params = {}

    def set_input_params(self, input_params):
        self.input_params = input_params

    def compile_file(self, file_path, compile_path, ext_vars, **kwargs):
        """
        Write file_path (kadet evaluated) items as files to compile_path.
        ext_vars is not used in Kadet
        kwargs:
            output: default 'yaml', accepts 'json'
            prune: default False
            reveal: default False, set to reveal refs on compile
            target_name: default None, set to current target being compiled
            indent: default 2
        """
        output = kwargs.get("output", "yaml")
        prune = kwargs.get("prune", False)
        reveal = kwargs.get("reveal", False)
        target_name = kwargs.get("target_name", None)
        inventory_path = kwargs.get("inventory_path", None)
        indent = kwargs.get("indent", 2)

        input_params = self.input_params
        # set compile_path allowing kadet functions to have context on where files
        # are being compiled on the current kapitan run
        input_params["compile_path"] = compile_path
        # reset between each compile if kadet component is used multiple times
        self.input_params = {}

        # Must be done before exec_module (but is useless for Task based module)
        # These will be updated per target
        # XXX At the moment we have no other way of setting externals for modules...
        global search_paths
        search_paths = self.search_paths
        global inventory
        inventory = lambda: Dict(inventory_func(self.search_paths, target_name, inventory_path))  # noqa E731
        global inventory_global
        inventory_global = lambda: Dict(inventory_func(self.search_paths, None, inventory_path))  # noqa E731

        kadet_module, spec = module_from_path(file_path)
        sys.modules[spec.name] = kadet_module

        hook = KadetFinder(self.search_paths)
        sys.meta_path.append(hook)
        try:
            spec.loader.exec_module(kadet_module)
        finally:
            sys.meta_path.remove(hook)

        logger.debug("Kadet.compile_file: spec.name: %s", spec.name)

        if hasattr(kadet_module, "Task") and issubclass(kadet_module.Task, KadetTask):
            task = kadet_module.Task(target_name, self.search_paths, inventory_path)
            logger.debug("Kadet Task")
            output_obj = task.run(input_params)
        else:
            kadet_arg_spec = inspect.getfullargspec(kadet_module.main)
            logger.debug("Kadet main args: %s", kadet_arg_spec.args)

            if len(kadet_arg_spec.args) == 1:
                output_obj = kadet_module.main(input_params)
            elif len(kadet_arg_spec.args) == 0:
                output_obj = kadet_module.main()
            else:
                raise ValueError(f"Kadet {spec.name} main parameters not equal to 1 or 0")

        output_obj = _to_dict(output_obj)
        if prune:
            output_obj = prune_empty(output_obj)

        # Return None if output_obj has no output
        if not output_obj:
            return None

        for item_key, item_value in output_obj.items():
            # write each item to disk
            if output == "json":
                file_path = os.path.join(compile_path, "%s.%s" % (item_key, output))
                with CompiledFile(
                    file_path,
                    self.ref_controller,
                    mode="w",
                    reveal=reveal,
                    target_name=target_name,
                    indent=indent,
                ) as fp:
                    fp.write_json(item_value)
            elif output in ["yml", "yaml"]:
                file_path = os.path.join(compile_path, "%s.%s" % (item_key, output))
                with CompiledFile(
                    file_path,
                    self.ref_controller,
                    mode="w",
                    reveal=reveal,
                    target_name=target_name,
                    indent=indent,
                ) as fp:
                    fp.write_yaml(item_value)
            elif output == "plain":
                file_path = os.path.join(compile_path, "%s" % item_key)
                with CompiledFile(
                    file_path,
                    self.ref_controller,
                    mode="w",
                    reveal=reveal,
                    target_name=target_name,
                    indent=indent,
                ) as fp:
                    fp.write(item_value)
            else:
                raise ValueError(
                    f"Output type defined in inventory for {file_path} is neither 'json', 'yaml' nor 'plain'"
                )
            logger.debug("Pruned output for: %s", file_path)

    def default_output_type(self):
        return "yaml"


def _to_dict(obj):
    """
    recursively update obj should it contain other
    BaseObj values
    """
    if isinstance(obj, BaseObj):
        for k, v in obj.root.items():
            obj.root[k] = _to_dict(v)
        # BaseObj needs to return to_dict()
        return obj.root.to_dict()
    elif isinstance(obj, list):
        # create new instance to make sure this is a list and not a subclass,
        # as the YAML encoder does not supports subclasses.
        return [_to_dict(item) for item in obj]
    elif isinstance(obj, dict):
        # create new instance to make sure this is a dict and not a subclass,
        # as the YAML encoder does not supports subclasses.
        return {k: _to_dict(v) for k, v in obj.items()}

    # anything else, return itself
    return obj


class BaseObj(object):
    def __init__(self, **kwargs):
        """
        returns a BaseObj
        kwargs will be save into self.kwargs
        values in self.root are returned as dict via self.to_dict()
        """
        self.root = Dict()
        self.kwargs = Dict(kwargs)
        self.new()
        self.body()

    @classmethod
    def from_json(cls, file_path):
        """
        returns a BaseObj initialised with json content
        from file_path
        """
        with open(file_path) as fp:
            json_obj = json.load(fp)
            return cls.from_dict(json_obj)

    @classmethod
    def from_yaml(cls, file_path):
        """
        returns a BaseObj initialised with yaml content
        from file_path
        """
        with open(file_path) as fp:
            yaml_obj = yaml.safe_load(fp)
            return cls.from_dict(yaml_obj)

    @classmethod
    def from_dict(cls, dict_value):
        """
        returns a BaseObj initialise with dict_value
        """
        bobj = cls()
        bobj.root = Dict(dict_value)
        return bobj

    def update_root(self, file_path):
        """
        update self.root with YAML/JSON content in file_path
        raises CompileError if file_path does not end with .yaml, .yml or .json
        """
        with open(file_path) as fp:
            if file_path.endswith(".yaml") or file_path.endswith(".yml"):
                yaml_obj = yaml.safe_load(fp)
                _copy = dict(self.root)
                _copy.update(yaml_obj)
                self.root = Dict(_copy)

            elif file_path.endswith(".json"):
                json_obj = json.load(fp)
                _copy = dict(self.root)
                _copy.update(json_obj)
                self.root = Dict(_copy)
            else:
                raise CompileError("file_path is neither JSON or YAML: {}".format(file_path))

    def need(self, key, msg="key and value needed"):
        """
        requires that key is set in self.kwargs
        errors with msg if key not set
        """
        err_msg = '{}: "{}": {}'.format(self.__class__.__name__, key, msg)
        if key not in self.kwargs:
            raise CompileError(err_msg)

    def new(self):
        """
        initialise need()ed keys for
        a new BaseObj
        """
        pass

    def body(self):
        """
        set values/logic for self.root
        """
        pass

    def to_dict(self):
        """
        returns object dict
        """
        return _to_dict(self)


class KadetTask:
    def __init__(self, target_name, search_paths, inventory_path):
        self.target_name = target_name
        self.search_paths = search_paths
        self.inventory_path = inventory_path

    @property
    def inv(self):
        return self.inventory

    @property
    def params(self) -> dict:
        return self.inv["parameters"]

    @property
    def target(self) -> str:
        return self.params["kapitan"]["vars"]["target"]

    @cached_property
    def inventory(self):
        return inventory_func(self.search_paths, self.target_name, self.inventory_path)

    @cached_property
    def inventory_global(self):
        return inventory_func(self.search_paths, None, self.inventory_path)

    def find_in_search_path(self, input_path) -> Collection[str]:
        globbed_paths = [glob.glob(os.path.join(path, input_path)) for path in self.search_paths]
        return set(itertools.chain.from_iterable(globbed_paths))

    def run(self, input_params: dict) -> dict:
        raise NotImplementedError()
