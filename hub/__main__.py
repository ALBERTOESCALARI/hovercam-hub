import asyncio
import logging

from hub import config
from hub.agent import Agent


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(Agent(config.load()).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
