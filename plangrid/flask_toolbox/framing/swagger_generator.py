import copy
import re
from collections import namedtuple

from plangrid.flask_toolbox.framing import swagger_words as sw
from plangrid.flask_toolbox.framing.framer import USE_DEFAULT
from plangrid.flask_toolbox.framing.framer import HeaderApiKeyAuthenticator
from plangrid.flask_toolbox.framing.marshmallow_to_jsonschema import get_swagger_title
from plangrid.flask_toolbox.framing.marshmallow_to_jsonschema import headers_converter_registry as global_headers_converter_registry
from plangrid.flask_toolbox.framing.marshmallow_to_jsonschema import query_string_converter_registry as global_query_string_converter_registry
from plangrid.flask_toolbox.framing.marshmallow_to_jsonschema import request_body_converter_registry as global_request_body_converter_registry
from plangrid.flask_toolbox.framing.marshmallow_to_jsonschema import response_converter_registry as global_response_converter_registry
from plangrid.flask_toolbox.validation import Error


def _get_key(obj):
    """
    Returns the key for a JSONSchema object that we can use to make a $ref.

    We're just enforcing that objects all have a title for now.

    :param dict obj:
    :rtype: str
    """
    return obj[sw.title]


def _get_ref(key, path=('#', sw.definitions)):
    """
    Constructs a path for a JSONSchema $ref.

    #/definitions/MyObject
    '.__________.'._____.'
          |          |
        path        key

    :param str key:
    :param iterable[str] path:
    :rtype: str
    """
    return '/'.join(list(path) + [key])


def _flatten(schema):
    """
    Recursively flattens a JSONSchema to a dictionary of keyed JSONSchemas,
    replacing nested objects with a reference to that object.


    Example input::
        {
          'type': 'object',
          'title': 'x',
          'properties': {
            'a': {
              'type': 'object',
              'title': 'y',
              'properties': {'b': {'type': 'integer'}}
            }
          }
        }

    Example output::
        {
          'x': {
            'type': 'object',
            'title': 'x',
            'properties': {
              'a': {'$ref': '#/definitions/y'}
            }
          },
          'y': {
            'type': 'object',
            'title': 'y',
            'properties': {'b': {'type': 'integer'}}
          }
        }

    This is useful for decomposing complex object generated from Marshmallow
    into a definitions object in Swagger.

    :param dict schema:
    :rtype: tuple(dict, dict)
    :returns: A tuple where the first item is the input object with any nested
    objects replaces with references, and the second item is the flattened
    definitions dictionary.
    """
    schema = copy.deepcopy(schema)

    definitions = {}

    if schema[sw.type_] == sw.object_:
        _flatten_object(schema=schema, definitions=definitions)
        schema = {sw.ref: _get_ref(_get_key(schema))}
    elif schema[sw.type_] == sw.array:
        _flatten_array(schema=schema, definitions=definitions)

    return schema, definitions


def _flatten_object(schema, definitions):
    for field, obj in schema[sw.properties].items():
        if obj[sw.type_] == sw.object_:
            obj_key = _flatten_object(schema=obj, definitions=definitions)
            schema[sw.properties][field] = {sw.ref: _get_ref(obj_key)}
        elif obj[sw.type_] == sw.array:
            _flatten_array(schema=obj, definitions=definitions)

    key = _get_key(schema)
    definitions[key] = schema

    return key


def _flatten_array(schema, definitions):
    if schema[sw.items][sw.type_] == sw.object_:
        obj_key = _flatten_object(schema=schema[sw.items], definitions=definitions)
        schema[sw.items] = {sw.ref: _get_ref(obj_key)}
    elif schema[sw.items][sw.type_] == sw.array:
        _flatten_array(schema=schema[sw.items], definitions=definitions)


def _convert_jsonschema_to_list_of_parameters(obj, in_='query'):
    """
    Swagger is only _based_ on JSONSchema. Query string and header parameters
    are represented as list, not as an object. This converts a JSONSchema
    object (as return by the converters) to a list of parameters suitable for
    swagger.

    :param dict obj:
    :param str in_: 'query' or 'header'
    :rtype: list[dict]
    """
    parameters = []

    assert obj['type'] == 'object'

    required = obj.get('required', [])

    for name, prop in obj['properties'].items():
        parameter = copy.deepcopy(prop)
        parameter['required'] = name in required
        parameter['in'] = in_
        parameter['name'] = name
        parameters.append(parameter)

    return parameters


_PATH_REGEX = re.compile('<((?P<type>.+?):)?(?P<name>.+?)>')
_PathArgument = namedtuple('PathArgument', ['name', 'type'])


def _format_path_for_swagger(path):
    """
    Flask and Swagger represent paths differently - this parses a Flask path
    to its Swagger form. This also extracts what the arguments in the flask
    path are, so we can represent them as parameters in Swagger.

    :param str path:
    :rtype: tuple(str, tuple(_PathArgument))
    """
    matches = list(_PATH_REGEX.finditer(path))

    args = tuple(
        _PathArgument(
            name=match.group('name'),
            type=match.group('type') or 'string'
        )
        for match in matches
    )

    subbed_path = _PATH_REGEX.sub(
        repl=lambda match: '{{{}}}'.format(match.group('name')),
        string=path
    )
    return subbed_path, args


def _convert_header_api_key_authenticator(authenticator):
    """
    Converts a HeaderApiKeyAuthenticator object to a Swagger definition.

    :param HeaderApiKeyAuthenticator authenticator:
    :rtype: tuple(str, dict)
    :returns: Tuple where the first item is a name for the authenticator, and
    the second item is a Swagger definition for it.
    """
    key = authenticator.name
    definition = {
        sw.name: authenticator.header,
        sw.in_: sw.header,
        sw.type_: sw.api_key
    }
    return key, definition


class SwaggerV2Generator(object):
    """
    Generates a v2.0 Swagger specification from a Framer object.

    Not all things are retrievable from the Framer object, so this
    guy also needs some additional information to complete the job.

    :param str host:
        Host name or ip of the API. This is not that useful for generating a
        static specification that will be used across multiple hosts (i.e.
        PlanGrid folks, don't worry about this guy. We have to override it
        manually when initializing a client anyways.
    :param iterable(str) schemes: "http", "https", "ws", or "wss"
    :param iterable(str) consumes: Mime Types the API accepts
    :param iterable(str) produces: Mime Types the API returns

    :param ConverterRegistry query_string_converter_registry:
    :param ConverterRegistry request_body_converter_registry:
    :param ConverterRegistry headers_converter_registry:
    :param ConverterRegistry response_converter_registry:
        ConverterRegistrys that will be used to convert Marshmallow schemas
        to the corresponding types of swagger objects. These default to the
        global registries.

    """
    def __init__(
            self,
            host='http://default.dev.planfront.net',
            schemes=('http',),
            consumes=('application/json',),
            produces=('application/vnd.plangrid+json',),
            query_string_converter_registry=None,
            request_body_converter_registry=None,
            headers_converter_registry=None,
            response_converter_registry=None,

            # TODO Still trying to figure out how to get this from the framer
            # Flask error handling doesn't mesh well with Swagger responses,
            # and I'm trying to avoid building our own layer on top of Flask's
            # error handlers.
            default_response_schema=Error()
    ):
        self.host = host
        self.schemes = schemes
        self.consumes = consumes
        self.produces = produces

        self._query_string_converter = (
            query_string_converter_registry
            or global_query_string_converter_registry
        ).convert
        self._request_body_converter = (
            request_body_converter_registry
            or global_request_body_converter_registry
        ).convert
        self._headers_converter = (
            headers_converter_registry
            or global_headers_converter_registry
        ).convert
        self._response_converter = (
            response_converter_registry
            or global_response_converter_registry
        ).convert

        self.flask_converters_to_swagger_types = {
            'uuid': sw.string,
            'string': sw.string,
            'int': sw.integer,
            'float': sw.number
        }

        self.authenticator_converters = {
            HeaderApiKeyAuthenticator: _convert_header_api_key_authenticator
        }

        self.default_response_schema = default_response_schema

    def register_flask_converter_to_swagger_type(self, flask_converter, swagger_type):
        """
        Flask has "converters" that convert path arguments to a Python type.

        We need to map these to Swagger types. This allows additional flask
        converter types (they're pluggable!) to be mapped to Swagger types.

        Unknown Flask converters will default to string.

        :param str flask_converter:
        :param str swagger_type:
        """
        self.flask_converters_to_swagger_types[flask_converter] = swagger_type

    def register_authenticator_converter(self, authenticator_class, converter):
        """
        The Framer allows for custom Authenticators.

        If you have a custom Authenticator, you need to add a function that
        can convert that authenticator to a Swagger representation.

        That function should take a single positional argument, which is the
        authenticator instance to be converted, and it should return a tuple
        where the first item is a name to use for the Swagger security
        definition, and the second item is the definition itself.

        :param Type(Authenticator) authenticator_class:
        :param function converter:
        """
        self.authenticator_converters[authenticator_class] = converter

    def generate(self, framer):
        """
        Generates a Swagger specification from a Framer instance.

        :param Framer framer:
        :rtype: dict
        """
        default_authenticator = framer.default_authenticator
        security_definitions = self._get_security_definitions(
            paths=framer.paths,
            default_authenticator=default_authenticator
        )
        definitions = self._get_definitions(paths=framer.paths)
        paths = self._get_paths(paths=framer.paths)

        swagger = {
            sw.swagger: self._get_version(),
            sw.info: self._get_info(),
            sw.host: self._get_host(),
            sw.schemes: self._get_schemes(),
            sw.consumes: self._get_consumes(),
            sw.produces: self._get_produces(),
            sw.security_definitions: security_definitions,
            sw.paths: paths,
            sw.definitions: definitions
        }

        if default_authenticator:
            swagger[sw.security] = self._get_security(default_authenticator)

        return swagger

    def _get_version(self):
        return '2.0'

    def _get_host(self):
        return self.host

    def _get_info(self):
        # TODO: add all the parameters for populating info
        return {}

    def _get_schemes(self):
        return list(self.schemes)

    def _get_consumes(self):
        return list(self.consumes)

    def _get_produces(self):
        return list(self.produces)

    def _get_security(self, authenticator):
        klass = authenticator.__class__
        converter = self.authenticator_converters[klass]
        name, _ = converter(authenticator)
        return {name: []}

    def _get_security_definitions(self, paths, default_authenticator):
        security_definitions = {}

        authenticators = set(
            d.authenticator
            for d in self._iterate_path_definitions(paths=paths)
            if d.authenticator is not None
            and d.authenticator is not USE_DEFAULT
        )

        if default_authenticator is not None:
            authenticators.add(default_authenticator)

        for authenticator in authenticators:
            klass = authenticator.__class__
            converter = self.authenticator_converters[klass]
            key, definition = converter(authenticator)
            security_definitions[key] = definition

        return security_definitions

    def _get_paths(self, paths):
        path_definitions = {}

        for path, methods in paths.items():
            path_definition = {}

            swagger_path, path_args = _format_path_for_swagger(path)
            path_definitions[swagger_path] = path_definition

            if path_args:
                path_definition[sw.parameters] = [
                    {
                        sw.name: path_arg.name,
                        sw.required: True,
                        sw.in_: sw.path,
                        sw.type_: self.flask_converters_to_swagger_types[path_arg.type]

                    }
                    for path_arg in path_args
                ]

            for method, d in methods.items():
                responses_definition = {
                    sw.default: {
                        sw.schema: {
                            sw.ref: _get_ref(get_swagger_title(self.default_response_schema))
                        }
                    }
                }

                if d.marshal_schemas:
                    for status_code, schema in d.marshal_schemas.items():
                        response_definition = {
                            sw.schema: {sw.ref: _get_ref(get_swagger_title(schema))}
                        }

                        responses_definition[str(status_code)] = response_definition

                parameters_definition = []

                if d.query_string_schema:
                    parameters_definition.extend(
                        _convert_jsonschema_to_list_of_parameters(
                            self._query_string_converter(d.query_string_schema),
                            in_=sw.query
                        )
                    )

                if d.request_body_schema:
                    schema = d.request_body_schema

                    parameters_definition.append({
                        sw.name: schema.__class__.__name__,
                        sw.in_: sw.body,
                        sw.required: True,
                        sw.schema: {sw.ref: _get_ref(get_swagger_title(schema))}
                    })

                if d.headers_schema:
                    parameters_definition.extend(
                        _convert_jsonschema_to_list_of_parameters(
                            self._headers_converter(d.headers_schema),
                            in_=sw.header
                        )
                    )

                method_lower = method.lower()
                path_definition[method_lower] = {
                    sw.operation_id: get_swagger_title(d.func),
                    sw.responses: responses_definition
                }

                if d.func.__doc__:
                    path_definition[method_lower][sw.description] = d.func.__doc__

                if parameters_definition:
                    path_definition[method_lower][sw.parameters] = parameters_definition

                if d.authenticator is None:
                    path_definition[method_lower][sw.security] = {}
                elif d.authenticator is not USE_DEFAULT:
                    security = self._get_security(d.authenticator)
                    path_definition[method_lower][sw.security] = security

        return path_definitions

    def _get_definitions(self, paths):
        all_schemas = set()

        converted = []

        all_schemas.add(self.default_response_schema)
        converted.append(self._response_converter(self.default_response_schema))

        for d in self._iterate_path_definitions(paths=paths):
            if d.marshal_schemas:
                for schema in d.marshal_schemas.values():
                    if schema not in all_schemas:
                        converted.append(self._response_converter(schema))
                    all_schemas.add(schema)

            if d.request_body_schema:
                schema = d.request_body_schema

                if schema not in all_schemas:
                    converted.append(self._request_body_converter(schema))

                all_schemas.add(schema)

        flattened = {}

        for obj in converted:
            _, flattened_definitions = _flatten(obj)
            flattened.update(flattened_definitions)

        return flattened

    def _iterate_path_definitions(self, paths):
        for methods in paths.values():
            for definition in methods.values():
                yield definition
