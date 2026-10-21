import procrastinate

from .config import get_settings

app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(conninfo=get_settings().database_url),
)
