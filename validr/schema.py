import json

from ._schema import (
    MarkIndex, MarkKey, dict_validator, list_validator, merge_validators
)
from .exceptions import Invalid, SchemaError
from .validators import builtin_validators


class ValidatorString:
    """
    ValidatorString

    eg::

        key?validator(args,args)&k&k=v
        key@xx@yy(args,args)&k&k=v

    Note: don't contain ',)' in args and '&=' in kwargs
    """

    def __init__(self, text):

        if text is None:
            raise SchemaError("can't parse None")

        # first: name, key?name, @refer or key@refer@refer
        # last: (args,args)&k&k=v or &k&k=v
        cuts = [text.find("("), text.find("&"), len(text)]
        cut = min([x for x in cuts if x >= 0])
        first, last = text[:cut], text[cut:]
        self.init_first_part(first)
        self.init_last_part(last)

    def init_first_part(self, first):
        key = name = refers = None
        if first:
            if ("?" in first and "@" in first) or first[-1] in "?@":
                raise SchemaError("invalid syntax %s" % repr(first))

            if "@" in first:
                # key@refer@refer / key@@refer
                items = first.split("@")
                if items[0]:
                    key = items[0]
                refers = items[1:]
                if not all(refers):
                    raise SchemaError("invalid syntax %s" % repr(first))
            else:
                # key, key?name / key?name?name
                items = first.split("?")
                if len(items) == 2:
                    key, name = items
                elif len(items) == 1:
                    name = items[0]
                else:
                    raise SchemaError("invalid syntax %s" % repr(first))
        self.key = key
        self.name = name
        self.refers = refers

    def init_last_part(self, last):
        text_args = text_kwargs = None
        if last and last[0] == "(":
            cut = last.find(")")
            if cut < 0:
                raise SchemaError("missing ')'")
            text_args = last[1:cut].rstrip(' ,')
            last = last[cut + 1:]
        if last:
            text_kwargs = last[1:]
        self.args = self.parse_args(text_args)
        self.kwargs = self.parse_kwargs(text_kwargs)

    def parse_args(self, text):
        if not text:
            return tuple()
        args = []
        for x in text.split(","):
            try:
                args.append(json.loads(x))
            except ValueError:
                raise SchemaError("invalid JSON value in %s" % repr(text))
        return tuple(args)

    def parse_kwargs(self, text):
        if not text:
            return {}
        kwargs = {}
        for kv in text.split("&"):
            cut = kv.find("=")
            if cut >= 0:
                try:
                    kwargs[kv[:cut]] = json.loads(kv[cut + 1:])
                except ValueError:
                    raise SchemaError("invalid JSON value in %s" % repr(kv))
            else:
                kwargs[kv] = True
        return kwargs

    def __repr__(self):
        return repr({
            "key": self.key,
            "name": self.name,
            "refers": self.refers,
            "args": self.args,
            "kwargs": self.kwargs
        })


def schema_key(k):
    cut = k.find("?")
    if cut < 0:
        cut = k.find("@")
    if cut > 0:
        return k[:cut]
    else:
        return k


class SchemaParser:
    """SchemaParser

    :param validators: custom validators
    :param shared: shared schema
    """

    def __init__(self, validators=None, shared=None):
        if validators is None:
            self.validators = {}
        else:
            self.validators = validators
        self.shared = {}
        if shared is not None:
            for k, v in shared.items():
                with MarkKey(k):
                    self.shared[k] = self.parse(v)

    def parse(self, schema):
        """Parse schema"""
        return self._parse(schema)

    def _parse_dict(self, schema):
        inner = {}
        vs = None
        for k, v in schema.items():
            with MarkKey(schema_key(k)):
                if k[:5] == "$self":
                    if vs is not None:
                        raise SchemaError("multi $self not allowed")
                    vs = ValidatorString(k)
                    vs.kwargs["desc"] = v
                else:
                    if isinstance(v, (dict, list)):
                        if any(char in k for char in"?@&()"):
                            raise SchemaError("invalid key %s" % repr(k))
                        inner[k] = self._parse(v)
                    else:
                        if "?" not in k and "@" not in k:
                            raise SchemaError("missing validator or refer")
                        inner_vs = ValidatorString(k)
                        inner[inner_vs.key] = self._parse(v, inner_vs)
        if vs:
            if not vs.refers:
                return dict_validator(inner, *vs.args, **vs.kwargs)
            else:
                # mixins
                _mixins = []
                for refer in vs.refers:
                    if refer not in self.shared:
                        raise SchemaError("shared '%s' not found" % refer)
                    _mixins.append(self.shared[refer])
                _mixins.append(dict_validator(inner))
                return merge_validators(_mixins, *vs.args, **vs.kwargs)
        else:
            return dict_validator(inner)

    def _parse_list(self, schema):
        vs = None
        if len(schema) == 1:
            schema = schema[0]
        elif len(schema) == 2:
            vs = ValidatorString(schema[0])
            schema = schema[1]
        else:
            raise SchemaError("invalid length of list schema")
        with MarkIndex(None):
            inner = self._parse(schema)
            if vs:
                return list_validator(inner, *vs.args, **vs.kwargs)
            else:
                return list_validator(inner)

    def _parse_scalar(self, schema, vs):
        if vs:
            vs.kwargs["desc"] = schema
        else:
            vs = ValidatorString(schema)
        if vs.refers:
            # refer
            if len(vs.refers) >= 2:
                raise SchemaError("multi refers not allowed")
            refer = vs.refers[0]
            if refer not in self.shared:
                raise SchemaError("shared '%s' not found" % refer)
            _validator = self.shared[refer]
            # refer optional
            if not vs.kwargs.get("optional"):
                return _validator
            else:
                def optional_refer_validator(value):
                    if value is None:
                        return None
                    else:
                        return _validator(value)
                return optional_refer_validator
        else:
            if vs.name in self.validators:
                validator = self.validators[vs.name]
            elif vs.name in builtin_validators:
                validator = builtin_validators[vs.name]
            else:
                raise SchemaError("validator '%s' not found" % vs.name)
            try:
                _validator = validator(*vs.args, **vs.kwargs)
            except TypeError as ex:
                raise SchemaError(str(ex))
            default = vs.kwargs.get("default", None)
            if default is not None:
                try:
                    _validator(default)
                except Invalid:
                    raise SchemaError("invalid default value")
            return _validator

    def _parse(self, schema, vs=None):
        """Parse schema

        :param schema: schema
        :param vs: ValidatorString
        """
        if isinstance(schema, dict):
            return self._parse_dict(schema)
        elif isinstance(schema, list):
            return self._parse_list(schema)
        else:
            return self._parse_scalar(schema, vs)
