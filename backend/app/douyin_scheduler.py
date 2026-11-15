from .simple_scheduler import SimpleTaskScheduler


class DouyinScheduler(SimpleTaskScheduler):
    def __init__(self, service):
        super().__init__(
            service,
            name="douyin.scheduler",
            thread_name="douyin-scheduler",
            label="抖音续火",
        )
