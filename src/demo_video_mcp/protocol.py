"""Small stdio MCP protocol adapter for Python 3.9 runtimes.

The application layer is isolated from this module so it can be replaced with
the official Python SDK when the runtime moves to Python 3.10+.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from . import __version__
from .errors import VideoMCPError


CURRENT_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
}


class ProtocolError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class StdioMCPServer:
    def __init__(
        self,
        *,
        name: str,
        instructions: str,
        tools: List[Dict[str, Any]],
        call_tool: Callable[[str, Mapping[str, Any]], Dict[str, Any]],
        list_resources: Callable[[], List[Dict[str, Any]]],
        read_resource: Callable[[str], Dict[str, Any]],
        list_prompts: Callable[[], List[Dict[str, Any]]],
        get_prompt: Callable[[str, Mapping[str, Any]], Dict[str, Any]],
    ):
        self.name = name
        self.instructions = instructions
        self.tools = tools
        self.call_tool_handler = call_tool
        self.list_resources_handler = list_resources
        self.read_resource_handler = read_resource
        self.list_prompts_handler = list_prompts
        self.get_prompt_handler = get_prompt
        self.initialized = False

    @staticmethod
    def _response(request_id: Any, result: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(
        request_id: Any,
        code: int,
        message: str,
        data: Any = None,
    ) -> Dict[str, Any]:
        error: Dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}

    @staticmethod
    def _tool_result(value: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                }
            ],
            "structuredContent": dict(value),
            "isError": False,
        }

    @staticmethod
    def _tool_error(message: str) -> Dict[str, Any]:
        return {
            "content": [{"type": "text", "text": message}],
            "isError": True,
        }

    def _initialize(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        requested = params.get("protocolVersion")
        version = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else CURRENT_PROTOCOL_VERSION
        )
        self.initialized = True
        return {
            "protocolVersion": version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {
                    "subscribe": False,
                    "listChanged": False,
                },
                "prompts": {"listChanged": False},
                "logging": {},
            },
            "serverInfo": {
                "name": self.name,
                "version": __version__,
            },
            "instructions": self.instructions,
        }

    def _dispatch(
        self, method: str, params: Mapping[str, Any]
    ) -> Dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.tools}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str):
                raise ProtocolError(-32602, "tool name is required")
            if not isinstance(arguments, dict):
                raise ProtocolError(-32602, "tool arguments must be an object")
            known = {tool["name"] for tool in self.tools}
            if name not in known:
                raise ProtocolError(-32602, f"unknown tool: {name}")
            try:
                value = self.call_tool_handler(name, arguments)
                return self._tool_result(value)
            except VideoMCPError as error:
                return self._tool_error(str(error))
            except Exception:
                traceback.print_exc(file=sys.stderr)
                return self._tool_error(
                    "Internal tool error. Check the MCP server stderr log."
                )
        if method == "resources/list":
            return {"resources": self.list_resources_handler()}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str):
                raise ProtocolError(-32602, "resource uri is required")
            try:
                return self.read_resource_handler(uri)
            except VideoMCPError as error:
                raise ProtocolError(-32602, str(error)) from error
        if method == "prompts/list":
            return {"prompts": self.list_prompts_handler()}
        if method == "prompts/get":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str):
                raise ProtocolError(-32602, "prompt name is required")
            if not isinstance(arguments, dict):
                raise ProtocolError(
                    -32602, "prompt arguments must be an object"
                )
            try:
                return self.get_prompt_handler(name, arguments)
            except VideoMCPError as error:
                raise ProtocolError(-32602, str(error)) from error
        if method == "logging/setLevel":
            return {}
        raise ProtocolError(-32601, f"method not found: {method}")

    def handle_message(
        self, message: Mapping[str, Any]
    ) -> Optional[Dict[str, Any]]:
        if message.get("jsonrpc") != "2.0":
            raise ProtocolError(-32600, "jsonrpc must be 2.0")
        method = message.get("method")
        if not isinstance(method, str):
            raise ProtocolError(-32600, "method is required")
        request_id = message.get("id")
        if "id" not in message:
            if method == "notifications/initialized":
                self.initialized = True
            return None
        params = message.get("params", {})
        if not isinstance(params, dict):
            return self._error(
                request_id,
                -32602,
                "params must be an object",
            )
        try:
            result = self._dispatch(method, params)
            return self._response(request_id, result)
        except ProtocolError as error:
            return self._error(request_id, error.code, error.message)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return self._error(
                request_id,
                -32603,
                "internal MCP server error",
            )

    def serve_forever(self) -> None:
        for raw_line in sys.stdin.buffer:
            request_id = None
            try:
                message = json.loads(raw_line.decode("utf-8"))
                if not isinstance(message, dict):
                    raise ProtocolError(-32600, "request must be an object")
                request_id = message.get("id")
                response = self.handle_message(message)
            except json.JSONDecodeError:
                response = self._error(None, -32700, "parse error")
            except ProtocolError as error:
                response = self._error(
                    request_id,
                    error.code,
                    error.message,
                )
            except Exception:
                traceback.print_exc(file=sys.stderr)
                response = self._error(
                    request_id,
                    -32603,
                    "internal MCP server error",
                )
            if response is not None:
                payload = json.dumps(
                    response,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                sys.stdout.write(payload + "\n")
                sys.stdout.flush()
