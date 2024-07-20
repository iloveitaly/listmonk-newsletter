import os

from apscheduler.schedulers.background import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from listmonk_newsletter import generate_campaign


def job():
    generate_campaign()


def cron():
    schedule = os.environ.get("SCHEDULE", "0 6 * * 1")
    print(f"Running on schedule: {schedule}")

    scheduler = BlockingScheduler()
    scheduler.add_job(job, CronTrigger.from_crontab(schedule))
    scheduler.start()


if __name__ == "__main__":
    cron()
