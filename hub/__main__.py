import asyncio
import logging

from hub import config
from hub.agent import Agent
from hub.updater import RestartRequested


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(Agent(config.load()).run())
    except RestartRequested:
        # New code was pulled; exit so the start script launches the new version.
        logging.getLogger("hovercam").info("exiting for update")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
