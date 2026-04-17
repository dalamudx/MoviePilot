import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI

from app.log import LogEntry, NonBlockingFileHandler


class FakeRotatingHandler:
    def __init__(self):
        self.handle_calls = []
        self.emit_calls = []
        self.close_called = False
        self.formatter = None
        self.stream = MagicMock()
        self.stream.closed = False

    def setFormatter(self, formatter):
        self.formatter = formatter

    def handle(self, record):
        self.handle_calls.append(record)

    def emit(self, record):
        self.emit_calls.append(record)

    def close(self):
        self.close_called = True
        self.stream.closed = True


class FakeClosedRotatingHandler(FakeRotatingHandler):
    def __init__(self):
        super().__init__()
        self.stream.closed = True


class FakeBrokenRotatingHandler(FakeRotatingHandler):
    def handle(self, record):
        raise OSError(5, "Input/output error")


class LogShutdownTest(unittest.TestCase):
    def setUp(self):
        NonBlockingFileHandler._instance = None
        NonBlockingFileHandler._rotating_handlers = {}
        self.handler = NonBlockingFileHandler()

    def tearDown(self):
        self.handler.shutdown()
        NonBlockingFileHandler._instance = None
        NonBlockingFileHandler._rotating_handlers = {}

    def test_shutdown_is_idempotent(self):
        self.handler.shutdown()
        self.handler.shutdown()

    def test_write_log_is_ignored_after_shutdown(self):
        with patch.object(self.handler, "_write_sync") as mock_write_sync:
            self.handler.shutdown()
            self.handler.write_log("INFO", "late log", Path("/tmp/test.log"))
            mock_write_sync.assert_not_called()

    def test_sync_write_uses_handler_handle(self):
        fake_handler = FakeRotatingHandler()
        entry = LogEntry("INFO", "hello", Path("/tmp/test.log"))

        with patch.object(
            self.handler, "_get_rotating_handler", return_value=fake_handler
        ):
            self.handler._write_sync(entry)

        self.assertEqual(1, len(fake_handler.handle_calls))
        self.assertEqual(0, len(fake_handler.emit_calls))

    def test_shutdown_closes_cached_handlers(self):
        fake_handler = FakeRotatingHandler()
        self.handler._rotating_handlers[Path("/tmp/test.log")] = fake_handler

        self.handler.shutdown()

        self.assertTrue(fake_handler.close_called)

    def test_get_rotating_handler_recreates_closed_cached_handler(self):
        file_path = Path("/tmp/test.log")
        closed_handler = FakeClosedRotatingHandler()
        replacement_handler = FakeRotatingHandler()
        self.handler._rotating_handlers[file_path] = closed_handler

        with patch("app.log.RotatingFileHandler", return_value=replacement_handler):
            handler = self.handler._get_rotating_handler(file_path)

        self.assertIs(handler, replacement_handler)
        self.assertTrue(closed_handler.close_called)

    def test_write_sync_recreates_handler_after_oserror(self):
        file_path = Path("/tmp/test.log")
        broken_handler = FakeBrokenRotatingHandler()
        replacement_handler = FakeRotatingHandler()
        entry = LogEntry("INFO", "hello", file_path)

        self.handler._rotating_handlers[file_path] = broken_handler

        with patch("app.log.RotatingFileHandler", return_value=replacement_handler):
            self.handler._write_sync(entry)

        self.assertTrue(broken_handler.close_called)
        self.assertEqual(1, len(replacement_handler.handle_calls))


class LifespanShutdownTest(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_stops_system_and_shuts_down_logger(self):
        app = FastAPI()

        lifecycle_module_name = "app.startup.lifecycle"
        sys.modules.pop(lifecycle_module_name, None)

        mock_system_chain = MagicMock()
        mock_global_vars = MagicMock()
        mock_logger = MagicMock()
        mock_system_helper = MagicMock()

        stub_modules = {
            "app.chain.system": types.SimpleNamespace(
                SystemChain=lambda: mock_system_chain
            ),
            "app.core.config": types.SimpleNamespace(global_vars=mock_global_vars),
            "app.log": types.SimpleNamespace(logger=mock_logger),
            "app.helper.system": types.SimpleNamespace(
                SystemHelper=lambda: mock_system_helper
            ),
            "app.startup.command_initializer": types.SimpleNamespace(
                init_command=MagicMock(),
                stop_command=MagicMock(),
                restart_command=MagicMock(),
            ),
            "app.startup.modules_initializer": types.SimpleNamespace(
                init_modules=MagicMock(),
                stop_modules=AsyncMock(),
            ),
            "app.startup.monitor_initializer": types.SimpleNamespace(
                stop_monitor=MagicMock(),
                init_monitor=MagicMock(),
            ),
            "app.startup.plugins_initializer": types.SimpleNamespace(
                init_plugins=MagicMock(),
                stop_plugins=MagicMock(),
                sync_plugins=AsyncMock(return_value=False),
            ),
            "app.startup.routers_initializer": types.SimpleNamespace(
                init_routers=MagicMock()
            ),
            "app.startup.scheduler_initializer": types.SimpleNamespace(
                stop_scheduler=MagicMock(),
                init_scheduler=MagicMock(),
                init_plugin_scheduler=MagicMock(),
            ),
            "app.startup.workflow_initializer": types.SimpleNamespace(
                init_workflow=MagicMock(),
                stop_workflow=MagicMock(),
            ),
        }

        with patch.dict(sys.modules, stub_modules):
            lifecycle = importlib.import_module(lifecycle_module_name)

            with patch.object(lifecycle, "init_extra", AsyncMock(return_value=None)):
                async with lifecycle.lifespan(app):
                    pass

        mock_global_vars.set_loop.assert_called_once()
        mock_global_vars.stop_system.assert_called_once()
        mock_system_chain.restore_plugins.assert_called_once()
        mock_system_chain.backup_plugins.assert_called_once()
        mock_logger.shutdown.assert_called_once()
