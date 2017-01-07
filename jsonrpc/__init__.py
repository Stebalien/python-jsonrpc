import json
import asyncio
import websockets

# Look-aside table for RPC exports. This way, we don't have to touch the
# functions themselves.
_RPC_EXPORTS = {}

def connect(server):
    return Client(JsonRPC(), server)

class RPCError(Exception):
    def __init__(self, error):
        self.code = error.get("code", 0),
        self.data = error.get("data", None)
        self.message = error.get("message", "")
        super().__init__(self.message)

class JsonRPCMeta(type):
    def __new__(mcs, name, bases, dict_):
        export = {}
        for (name, method) in dict_.items():
            if method in _RPC_EXPORTS:
                remote_name = _RPC_EXPORTS[method]
                if remote_name is True:
                    remote_name = name
                export[remote_name] = name
        dict_["exports"] = export
        return type.__new__(mcs, name, bases, dict_)

class JsonRPC(object, metaclass=JsonRPCMeta):
    def connect(self, server):
        return Client(self, server)

class Client:
    def __init__(self, me, server):
        self.connection = None

        if not isinstance(me, JsonRPC):
            raise TypeError("`me' must be an instance of `JsonRPC'")

        self.me = me
        self.__connect = websockets.connect(server)

    async def __aenter__(self):
        self.connection = Connection(self.me, await self.__connect)

        return self.connection

    async def serve(self):
        async with self as connection:
            await connection.serve()

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.connection.close()

class Connection:
    def __init__(self, me, websocket):
        self.__event_loop = asyncio.get_event_loop()
        self.__me = me
        self.__next_id = 0
        self.__websocket = websocket
        self.__futures = {}
        self.__task = asyncio.ensure_future(self.__run())

    async def serve(self):
        await asyncio.shield(self.__task)

    async def close(self):
        if not self.__event_loop.is_closed():
            self.__task.cancel()
            await self.__websocket.close()

    async def call(self, method, *args):
        next_id = self.__next_id
        self.__next_id += 1

        future = self.__event_loop.create_future()
        self.__futures[next_id] = future
        await self.__websocket.send(json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "id": next_id,
            "params": args,
        }))
        result = await future
        return result

    async def __run(self):
        while True:
            msg = json.loads(await self.__websocket.recv())
            result = msg.get("result", None)
            error = msg.get("error", None)
            mid = msg.get("id", None)
            method = msg.get("method", None)

            if result is not None:
                if mid in self.__futures:
                    future = self.__futures.pop(mid)
                    if not future.cancelled():
                        future.set_result(result)
                else:
                    # Ignore invalid response?
                    pass
            elif error is not None:
                if mid in self.__futures:
                    future = self.__futures.pop(mid)
                    if not future.cancelled():
                        future.set_exception(RPCError(error))
                else:
                    # Ignore invalid response?
                    pass
            elif method is not None:
                params = msg.get("params", [])
                asyncio.ensure_future(self.__handle_call(method, params, mid))
            else:
                asyncio.ensure_future(self.__send_error(mid, -32600, "Invalid Request", msg))

    async def __send_error(self, mid, code, message, data=None):
        await self.__websocket.send(json.dumps({
            "jsonrpc": "2.0",
            "id": mid,
            "error": {
                "code": code,
                "message": message,
                "data": data,
            },
        }))

    async def __handle_call(self, method_name, params, mid):
        local_name = type(self.__me).exports.get(method_name, None)
        if local_name is None:
            if mid is not None:
                await self.__send_error(mid, -32601, "No such method")
            return

        method = getattr(self.__me, local_name)

        try:
            if asyncio.iscoroutinefunction(method):
                result = await method(self, *params)
            else:
                result = method(self, *params)
            if mid is not None:
                await self.__websocket.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": result,
                }))
        except RPCError as e:
            if mid is not None:
                await self.__send_error(mid, e.code, e.message, e.data)
        except Exception as e: #pylint: disable=broad-except
            if mid is not None:
                await self.__send_error(mid, -1, str(e))

    def __getattr__(self, name):
        return Proxy(self, name)

class Proxy:
    def __init__(self, con, method):
        self.__connection = con
        self.__method = method

    def __getattr__(self, name):
        return Proxy(self.__connection, self.__method + "." + name)

    async def __call__(self, *args):
        return await self.__connection.call(self.__method, *args)

def bind(name_or_func):
    if callable(name_or_func):
        _RPC_EXPORTS[name_or_func] = True
        return name_or_func
    else:
        def wrap(func):
            _RPC_EXPORTS[func] = name_or_func
            return func
        return wrap
