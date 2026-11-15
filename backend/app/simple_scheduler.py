"""简易任务调度线程。

秒应与 AI工具签 的调度循环完全同构（每 2 秒唤醒一次执行到期任务），
统一为 SimpleTaskScheduler；两平台的调度器类退化为薄壳以保留原命名。
班级魔方需要线程池 + 抢占语义，仍使用独立的 ClassCubeScheduler。
"""

import logging
import threading


class SimpleTaskScheduler:
    def __init__(self, service, *, name: str, thread_name: str, label: str):
        self.service = service
        self._label = label
        self._stop = threading.Event()
        self._thread = None
        self.logger = logging.getLogger(name)
        self._thread_name = thread_name

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=self._thread_name, daemon=True
        )
        self._thread.start()

    def _loop(self):
        while not self._stop.wait(2):
            try:
                self.service.run_due_tasks()
            except Exception as exc:
                self.logger.error("%s调度扫描失败：%s", self._label, exc)

    def shutdown(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None
