from alembic import context
from database import Base, get_engine
import models  # noqa: F401 -- populate Base.metadata


def run(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


provided_connection = context.config.attributes.get("connection")
if provided_connection is not None:
    run(provided_connection)
else:
    with get_engine().connect() as connection:
        run(connection)
