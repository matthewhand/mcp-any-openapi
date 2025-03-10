"""
Provides the FastMCP server logic for mcp-openapi-proxy.

This server exposes a pre-defined set of functions based on an OpenAPI specification.
Configuration is controlled via environment variables:

- OPENAPI_SPEC_URL_<hash>: Unique URL per test, falls back to OPENAPI_SPEC_URL.
- TOOL_WHITELIST: Comma-separated list of allowed endpoint paths.
- SERVER_URL_OVERRIDE: Optional override for the base URL from the OpenAPI spec.
- API_KEY: Generic token for Bearer header.
- STRIP_PARAM: Param name (e.g., "auth") to remove from parameters.
"""

import os
import sys
import json
import requests
from typing import Dict, Any
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp_openapi_proxy.utils import setup_logging, is_tool_whitelisted, fetch_openapi_spec, build_base_url, normalize_tool_name, handle_auth, strip_parameters, get_tool_prefix

logger = setup_logging(debug=os.getenv("DEBUG", "").lower() in ("true", "1", "yes"))

logger.debug(f"Server CWD: {os.getcwd()}")
logger.debug(f"Server sys.path: {sys.path}")

mcp = FastMCP("OpenApiProxy-Fast")

@mcp.tool()
def list_functions(*, env_key: str = "OPENAPI_SPEC_URL") -> str:
    """Lists available functions derived from the OpenAPI specification."""
    logger.debug("Executing list_functions tool.")
    spec_url = os.environ.get(env_key, os.environ.get("OPENAPI_SPEC_URL"))
    whitelist = os.getenv('TOOL_WHITELIST')
    logger.debug(f"Using spec_url: {spec_url}")
    logger.debug(f"TOOL_WHITELIST value: {whitelist}")
    if not spec_url:
        logger.error("No OPENAPI_SPEC_URL or custom env_key configured.")
        return json.dumps([])
    spec = fetch_openapi_spec(spec_url)
    if spec is None:
        logger.error("Spec is None after fetch_openapi_spec")
        return json.dumps([])
    logger.debug(f"Raw spec loaded: {json.dumps(spec, indent=2)}")
    paths = spec.get("paths", {})
    logger.debug(f"Paths extracted from spec: {list(paths.keys())}")
    if not paths:
        logger.debug("No paths found in spec.")
        return json.dumps([])
    functions = {}
    prefix = get_tool_prefix()
    for path, path_item in paths.items():
        logger.debug(f"Processing path: {path}")
        if not path_item:
            logger.debug(f"Path item is empty for {path}")
            continue
        whitelist_check = is_tool_whitelisted(path)
        logger.debug(f"Whitelist check for {path}: {whitelist_check}")
        if not whitelist_check:
            logger.debug(f"Path {path} not in whitelist - skipping.")
            continue
        for method, operation in path_item.items():
            logger.debug(f"Found method: {method} for path: {path}")
            if not method:
                logger.debug(f"Method is empty for {path}")
                continue
            if method.lower() not in ["get", "post", "put", "delete", "patch"]:
                logger.debug(f"Method {method} not supported for {path} - skipping.")
                continue
            raw_name = f"{method.upper()} {path}"
            function_name = normalize_tool_name(raw_name)
            if prefix:
                function_name = f"{prefix}{function_name}"
            if function_name in functions:
                logger.debug(f"Skipping duplicate tool name: {function_name}")
                continue
            function_description = operation.get("summary", operation.get("description", "No description provided."))
            logger.debug(f"Registering function: {function_name} - {function_description}")
            input_schema = {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False
            }
            for param in operation.get("parameters", []):
                param_name = param.get("name")
                param_type = param.get("type", "string")
                if param_type not in ["string", "integer", "boolean", "number"]:
                    param_type = "string"
                input_schema["properties"][param_name] = {
                    "type": param_type,
                    "description": param.get("description", f"{param.get('in', 'unknown')} parameter {param_name}")
                }
                if param.get("required", False):
                    input_schema["required"].append(param_name)
            functions[function_name] = {
                "name": function_name,
                "description": function_description,
                "path": path,
                "method": method.upper(),
                "operationId": operation.get("operationId"),
                "original_name": raw_name,
                "inputSchema": input_schema
            }
    logger.info(f"Discovered {len(functions)} functions from the OpenAPI specification.")
    logger.debug(f"Functions list: {list(functions.values())}")
    return json.dumps(list(functions.values()), indent=2)

@mcp.tool()
def call_function(*, function_name: str, parameters: dict = None, env_key: str = "OPENAPI_SPEC_URL") -> str:
    """Calls a function derived from the OpenAPI specification."""
    logger.debug(f"call_function invoked with function_name='{function_name}' and parameters={parameters}")
    logger.debug(f"API_KEY: {os.getenv('API_KEY', '<not set>')[:5] + '...' if os.getenv('API_KEY') else '<not set>'}")
    logger.debug(f"STRIP_PARAM: {os.getenv('STRIP_PARAM', '<not set>')}")
    if not function_name:
        logger.error("function_name is empty or None")
        return json.dumps({"error": "function_name is required"})
    spec_url = os.environ.get(env_key, os.environ.get("OPENAPI_SPEC_URL"))
    if not spec_url:
        logger.error("No OPENAPI_SPEC_URL or custom env_key configured.")
        return json.dumps({"error": "OPENAPI_SPEC_URL is not configured"})
    spec = fetch_openapi_spec(spec_url)
    if spec is None:
        logger.error("Spec is None for call_function")
        return json.dumps({"error": "Failed to fetch or parse the OpenAPI specification"})
    logger.debug(f"Spec keys for call_function: {list(spec.keys())}")
    function_def = None
    paths = spec.get("paths", {})
    logger.debug(f"Paths for function lookup: {list(paths.keys())}")
    prefix = get_tool_prefix()
    for path, path_item in paths.items():
        logger.debug(f"Checking path: {path}")
        for method, operation in path_item.items():
            logger.debug(f"Checking method: {method} for path: {path}")
            if method.lower() not in ["get", "post", "put", "delete", "patch"]:
                logger.debug(f"Skipping unsupported method: {method}")
                continue
            raw_name = f"{method.upper()} {path}"
            current_function_name = normalize_tool_name(raw_name)
            if prefix:
                current_function_name = f"{prefix}{current_function_name}"
            logger.debug(f"Comparing {current_function_name} with {function_name}")
            if current_function_name == function_name:
                function_def = {
                    "path": path,
                    "method": method.upper(),
                    "operation": operation
                }
                logger.debug(f"Matched function definition for '{function_name}': {function_def}")
                break
        if function_def:
            break
    if not function_def:
        logger.error(f"Function '{function_name}' not found in the OpenAPI specification.")
        return json.dumps({"error": f"Function '{function_name}' not found"})
    logger.debug(f"Function def found: {function_def}")

    operation = function_def["operation"]
    operation["method"] = function_def["method"]
    headers = handle_auth(operation)
    parameters = strip_parameters(parameters)
    if function_def["method"] != "GET":
        headers["Content-Type"] = "application/json"

    if not is_tool_whitelisted(function_def["path"]):
        logger.error(f"Access to function '{function_name}' is not allowed.")
        return json.dumps({"error": f"Access to function '{function_name}' is not allowed"})

    base_url = build_base_url(spec)
    if not base_url:
        logger.error("Failed to construct base URL from spec or SERVER_URL_OVERRIDE.")
        return json.dumps({"error": "No base URL defined in spec or SERVER_URL_OVERRIDE"})

    path = function_def["path"]
    api_url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    request_params = {}
    request_body = None

    if isinstance(parameters, dict):
        if "stream" in parameters and parameters["stream"]:
            del parameters["stream"]
        path_params_in_openapi = [
            param["name"] for param in operation.get("parameters", []) if param.get("in") == "path"
        ]
        if path_params_in_openapi:
            missing_required = [
                param["name"] for param in operation.get("parameters", [])
                if param.get("in") == "path" and param.get("required", False) and param["name"] not in parameters
            ]
            if missing_required:
                logger.error(f"Missing required path parameters: {missing_required}")
                return json.dumps({"error": f"Missing required path parameters: {missing_required}"})
            for param_name in path_params_in_openapi:
                if param_name in parameters:
                    api_url = api_url.replace(f"{{{param_name}}}", str(parameters.pop(param_name)))
                    logger.debug(f"Replaced path param {param_name} in URL: {api_url}")
        if function_def["method"] == "GET":
            request_params = parameters
        else:
            request_body = parameters
    else:
        parameters = {}
        logger.debug("No valid parameters provided, proceeding without params/body")

    logger.debug(f"Sending request - Method: {function_def['method']}, URL: {api_url}, Headers: {headers}, Params: {request_params}, Body: {request_body}")
    try:
        response = requests.request(
            method=function_def["method"],
            url=api_url,
            headers=headers,
            params=request_params if function_def["method"] == "GET" else None,
            json=request_body if function_def["method"] != "GET" else None
        )
        response.raise_for_status()
        logger.debug(f"API response received: {response.text}")
        return response.text
    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed: {e}", exc_info=True)
        return json.dumps({"error": f"API request failed: {e}"})

def run_simple_server():
    """Runs the FastMCP server."""
    logger.debug("Starting run_simple_server")
    spec_url = os.environ.get("OPENAPI_SPEC_URL")
    if not spec_url:
        logger.error("OPENAPI_SPEC_URL environment variable is required for FastMCP mode.")
        sys.exit(1)

    logger.debug("Preloading tools from OpenAPI spec...")
    spec = fetch_openapi_spec(spec_url)
    if spec is None:
        logger.error("Failed to fetch OpenAPI spec, no tools to preload.")
        sys.exit(1)
    list_functions()

    try:
        logger.debug("Starting MCP server (FastMCP version)...")
        mcp.run(transport="stdio")
    except Exception as e:
        logger.error(f"Unhandled exception in MCP server (FastMCP): {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    run_simple_server()
