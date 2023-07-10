import http.client
import json
import warnings
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Set, Tuple, cast

from pydantic.fields import FieldInfo
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue
from starlette.routing import BaseRoute
from starlette.status import HTTP_422_UNPROCESSABLE_ENTITY
from typing_extensions import Literal

from esmerald._openapi._utils import (
    deep_dict_update,
    get_definitions,
    get_schema_from_model_field,
    is_body_allowed_for_status_code,
    status_code_ranges,
    validation_error_definition,
    validation_error_response_definition,
)
from esmerald._openapi.constants import METHODS_WITH_BODY, REF_PREFIX, REF_TEMPLATE
from esmerald._openapi.datastructures import ResponseSpecification
from esmerald._openapi.models import Contact, License, OpenAPI, Server, Tag
from esmerald.params import Body, Param
from esmerald.routing import gateways, router
from esmerald.typing import ModelMap, Undefined
from esmerald.utils.constants import DATA
from esmerald.utils.url import clean_path

if TYPE_CHECKING:
    pass


def get_flat_params(route: BaseRoute) -> List[Any]:
    """Gets all the neded params of the request and route"""
    path_params = [param.field_info for param in route.transformer.get_path_params()]
    cookie_params = [param.field_info for param in route.transformer.get_cookie_params()]
    query_params = [param.field_info for param in route.transformer.get_query_params()]
    header_params = [param.field_info for param in route.transformer.get_header_params()]

    return path_params + query_params + cookie_params + header_params


def get_fields_from_routes(
    routes: Sequence[BaseRoute], request_fields: Optional[List[FieldInfo]] = None
) -> List[FieldInfo]:
    """Extracts the fields from the given routes of Esmerald"""
    body_fields: List[FieldInfo] = []
    response_from_routes: List[FieldInfo] = []
    request_fields: List[FieldInfo] = []

    for route in routes:
        if getattr(route, "include_in_schema", None) and isinstance(route, router.Include):
            request_fields.extend(get_fields_from_routes(route.routes, request_fields))
            continue

        if getattr(route, "include_in_schema", None) and isinstance(route, gateways.Gateway):
            if DATA in route.handler.signature_model.model_fields:
                data_field = route.handler.data_field
                body_fields.append(data_field)
            # if route.handler.responses:
            #     response_from_routes.append(route.handler.responses.values())
            params = get_flat_params(route.handler)
            if params:
                request_fields.extend(params)

    return list(body_fields + response_from_routes + request_fields)


def get_compat_model_name_map(all_fields: List[FieldInfo]) -> ModelMap:
    return {}


def get_openapi_operation(
    *, route: router.HTTPHandler, method: str, operation_ids: Set[str]
) -> Dict[str, Any]:
    operation: Dict[str, Any] = {}
    if route.tags:
        operation["tags"] = route.tags
    operation["summary"] = route.summary or route.name.replace("_", " ").title()

    if route.description:
        operation["description"] = route.description

    operation_id = route.operation_id
    if operation_id in operation_ids:
        message = (
            f"Duplicate Operation ID {operation_id} for function " + f"{route.endpoint.__name__}"
        )
        file_name = getattr(route.endpoint, "__globals__", {}).get("__file__")
        if file_name:
            message += f" at {file_name}"
        warnings.warn(message, stacklevel=1)
    operation_ids.add(operation_id)
    operation["operationId"] = operation_id
    if route.deprecated:
        operation["deprecated"] = route.deprecated
    return operation


def get_openapi_operation_parameters(
    *,
    all_route_params: Sequence[FieldInfo],
    schema_generator: GenerateJsonSchema,
    model_name_map: ModelMap,
    field_mapping: Dict[Tuple[FieldInfo, Literal["validation", "serialization"]], JsonSchemaValue],
) -> List[Dict[str, Any]]:
    parameters = []
    for param in all_route_params:
        field_info = cast(Param, param)
        if not field_info.include_in_schema:
            continue

        param_schema = get_schema_from_model_field(
            field=param,
            schema_generator=schema_generator,
            model_name_map=model_name_map,
            field_mapping=field_mapping,
        )
        parameter = {
            "name": param.alias,
            "in": field_info.in_.value,
            "required": param.is_required(),
            "schema": param_schema,
        }
        if field_info.description:
            parameter["description"] = field_info.description
        if field_info.example != Undefined:
            parameter["example"] = json.dumps(field_info.example)
        if field_info.deprecated:
            parameter["deprecated"] = field_info.deprecated
        parameters.append(parameter)
    return parameters


def get_openapi_operation_request_body(
    *,
    data_field: Optional[FieldInfo],
    schema_generator: GenerateJsonSchema,
    model_name_map: ModelMap,
    field_mapping: Dict[Tuple[FieldInfo, Literal["validation", "serialization"]], JsonSchemaValue],
) -> Optional[Dict[str, Any]]:
    if not data_field:
        return None

    assert isinstance(data_field, FieldInfo), "The 'data' needs to be a FieldInfo"
    schema = get_schema_from_model_field(
        field=data_field,
        schema_generator=schema_generator,
        model_name_map=model_name_map,
        field_mapping=field_mapping,
    )

    field_info = cast(Body, data_field)
    request_media_type = field_info.media_type.value
    required = field_info.is_required()

    request_data_oai: Dict[str, Any] = {}
    if required:
        request_data_oai["required"] = required

    request_media_content: Dict[str, Any] = {"schema": schema}
    if field_info.example != Undefined:
        request_media_content["example"] = json.dumps(field_info.example)
    request_data_oai["content"] = {request_media_type: request_media_content}
    return request_data_oai


def get_openapi_path(
    *,
    route: gateways.Gateway,
    operation_ids: Set[str],
    schema_generator: GenerateJsonSchema,
    model_name_map: ModelMap,
    field_mapping: Dict[Tuple[FieldInfo, Literal["validation", "serialization"]], JsonSchemaValue],
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    path = {}
    security_schemes: Dict[str, Any] = {}
    definitions: Dict[str, Any] = {}

    assert route.methods is not None, "Methods must be a list"

    route_response_media_type: str = None
    # response_class = route.handler.response_class
    # if not response_class:
    #     response_class = route.handler.signature.return_annotation

    # if issubclass(response_class, BaseModel):
    #     _response_class = response_class()
    #     route_response_media_type = _response_class.media_type
    # else:
    #     route_response_media_type = _response_class.media_type

    handler = route.handler

    # If routes do not want to be included in the schema generation
    if not route.include_in_schema or not handler.include_in_schema:
        return path, security_schemes, definitions

    # For each method
    for method in route.methods:
        operation = get_openapi_operation(
            route=handler, method=method, operation_ids=operation_ids
        )
        parameters: List[Dict[str, Any]] = []
        security_definitions = {}
        for security in handler.security:
            security_definitions[security.name] = security.model_dump(
                by_alias=True, exclude_none=True
            )

        if security_definitions:
            security_schemes.update(security_definitions)

        all_route_params = get_flat_params(handler)
        operation_parameters = get_openapi_operation_parameters(
            all_route_params=all_route_params,
            schema_generator=schema_generator,
            model_name_map=model_name_map,
            field_mapping=field_mapping,
        )
        parameters.extend(operation_parameters)

        if parameters:
            all_parameters = {(param["in"], param["name"]): param for param in parameters}
            required_parameters = {
                (param["in"], param["name"]): param
                for param in parameters
                if param.get("required")
            }
            all_parameters.update(required_parameters)
            operation["parameters"] = list(all_parameters.values())

        if method in METHODS_WITH_BODY:
            request_data_oai = get_openapi_operation_request_body(
                data_field=handler.data_field,
                schema_generator=schema_generator,
                model_name_map=model_name_map,
                field_mapping=field_mapping,
            )
            if request_data_oai:
                operation["requestBody"] = request_data_oai

        status_code = str(handler.status_code)
        operation.setdefault("responses", {}).setdefault(status_code, {})[
            "description"
        ] = handler.response_description

        # Media type
        if route_response_media_type and is_body_allowed_for_status_code(handler.status_code):
            response_schema = {"type": "string"}
            response_schema = {}

            operation.setdefault("responses", {}).setdefault(route_response_media_type, {})[
                "schema"
            ] = response_schema

        # Additional responses
        if handler.responses:
            operation_responses = operation.setdefault("responses", {})
            for additional_status_code, additional_response in handler.responses.items():
                process_response = additional_response.model_copy()
                status_code_key = str(additional_status_code).upper()

                if status_code_key == "DEFAULT":
                    status_code_key = "default"

                openapi_response = operation_responses.setdefault(status_code_key, {})

                assert isinstance(
                    process_response, ResponseSpecification
                ), "An additional response must be an instance of ResponseSpecification"

                field = handler.responses.get(additional_status_code)
                additional_field_schema: Optional[Dict[str, Any]] = None

                if field:
                    additional_field_schema = field.model.model_json_schema()
                    media_type = route_response_media_type or "application/json"
                    additional_schema = (
                        process_response.model.model_json_schema()
                        .setdefault("content", {})
                        .setdefault(media_type, {})
                        .setdefault("schema", {})
                    )
                    deep_dict_update(additional_schema, additional_field_schema)

                # status
                status_text = (
                    process_response.status_text
                    or status_code_ranges.get(str(additional_status_code).upper())
                    or http.client.responses.get(int(additional_status_code))
                )
                description = (
                    process_response.description
                    or openapi_response.get("description")
                    or status_text
                    or "Additional Response"
                )

                deep_dict_update(openapi_response, process_response.model.model_json_schema())
                openapi_response["description"] = description
        http422 = str(HTTP_422_UNPROCESSABLE_ENTITY)
        if (all_route_params or handler.data_field) and not any(
            status in operation["responses"] for status in {http422, "4XX", "default"}
        ):
            operation["responses"][http422] = {
                "description": "Validation Error",
                "content": {
                    "application/json": {"schema": {"$ref": REF_PREFIX + "HTTPValidationError"}}
                },
            }
            if "ValidationError" not in definitions:
                definitions.update(
                    {
                        "ValidationError": validation_error_definition,
                        "HTTPValidationError": validation_error_response_definition,
                    }
                )
        path[method.lower()] = operation
    return path, security_schemes, definitions


def get_openapi(
    *,
    title: str,
    version: str,
    openapi_version: str = "3.1.0",
    summary: Optional[str] = None,
    description: Optional[str] = None,
    routes: Sequence[BaseRoute],
    tags: Optional[List[Tag]] = None,
    servers: Optional[List[Server]] = None,
    terms_of_service: Optional[str] = None,
    contact: Optional[Contact] = None,
    license: Optional[License] = None,
) -> Dict[str, Any]:
    """
    Builds the whole OpenAPI route structure and object
    """
    info: Dict[str, Any] = {"title": title, "version": version}
    if summary:
        info["summary"] = summary
    if description:
        info["description"] = description
    if terms_of_service:
        info["termsOfService"] = terms_of_service
    if contact:
        info["contact"] = contact
    if license:
        info["license"] = license
    output: Dict[str, Any] = {"openapi": openapi_version, "info": info}

    if servers:
        output["servers"] = servers

    components: Dict[str, Dict[str, Any]] = {}
    paths: Dict[str, Dict[str, Any]] = {}
    operation_ids: Set[str] = set()
    all_fields = get_fields_from_routes(list(routes or []))
    model_name_map = get_compat_model_name_map(all_fields)
    schema_generator = GenerateJsonSchema(ref_template=REF_TEMPLATE)
    field_mapping, definitions = get_definitions(
        fields=all_fields,
        schema_generator=schema_generator,
        model_name_map=model_name_map,
    )

    # Iterate through the routes
    def iterate_routes(
        routes: List[BaseRoute],
        definitions: Any = None,
        components: Any = None,
        prefix: Optional[str] = "",
    ):
        for route in routes:
            if isinstance(route, router.Include):
                definitions, components = iterate_routes(
                    route.routes, definitions, components, prefix=route.path
                )
                continue

            if isinstance(route, gateways.Gateway):
                result = get_openapi_path(
                    route=route,
                    operation_ids=operation_ids,
                    schema_generator=schema_generator,
                    model_name_map=model_name_map,
                    field_mapping=field_mapping,
                )
                if result:
                    path, security_schemes, path_definitions = result
                    if path:
                        route_path = clean_path(prefix + route.path_format)
                        paths.setdefault(route_path, {}).update(path)
                    if security_schemes:
                        components.setdefault("securitySchemes", {}).update(security_schemes)
                    if path_definitions:
                        definitions.update(path_definitions)

        return definitions, components

    definitions, components = iterate_routes(
        routes=routes, definitions=definitions, components=components
    )

    if definitions:
        components["schemas"] = {k: definitions[k] for k in sorted(definitions)}
    if components:
        output["components"] = components
    output["paths"] = paths
    if tags:
        output["tags"] = tags

    openapi = OpenAPI(**output)
    model_dump = openapi.model_dump(by_alias=True, exclude_none=True)
    return model_dump
