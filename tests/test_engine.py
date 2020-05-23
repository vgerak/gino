import pytest
from asyncpg import ConnectionDoesNotExistError
from sqlalchemy import text
from sqlalchemy.engine.url import URL
from sqlalchemy.exc import ObjectNotExecutableError, DBAPIError, InvalidRequestError

from gino.engine import AsyncConnection


async def test_connect_ctx(engine, db_val, get_db_val_sql):
    async with engine.connect() as conn:
        rows = await conn.execute(get_db_val_sql).all()

    assert rows[0][0] == db_val


async def test_connect_await(engine, db_val, get_db_val_sql):
    conn = await engine.connect()
    try:
        rows = await conn.execute(get_db_val_sql).all()

        assert rows[0][0] == db_val
    finally:
        await conn.close()


async def test_engine_begin(engine, db_val, get_db_val_sql, set_db_val_sql):
    with pytest.raises(Exception):
        async with engine.begin() as conn:
            await conn.execute(set_db_val_sql.bindparams(value=db_val + 1))
            raise Exception()

    async with engine.begin() as conn:
        await conn.execute(set_db_val_sql.bindparams(value=db_val - 1))

    async with engine.begin() as conn:
        rows = await conn.execute(get_db_val_sql).all()

    assert rows[0][0] == db_val - 1


async def test_conn_begin_ctx(engine, db_val, get_db_val_sql, set_db_val_sql):
    async with engine.connect() as conn:
        with pytest.raises(Exception):
            async with conn.begin():
                await conn.execute(set_db_val_sql.bindparams(value=db_val + 1))
                raise Exception()

        async with conn.begin():
            await conn.execute(set_db_val_sql.bindparams(value=db_val - 1))

        async with conn.begin() as tx:
            rows = await conn.execute(get_db_val_sql).all()

            with pytest.raises(InvalidRequestError, match="already started"):
                await tx
            with pytest.raises(InvalidRequestError, match="is managed"):
                await tx.commit()
            with pytest.raises(InvalidRequestError, match="is managed"):
                await tx.rollback()

        assert rows[0][0] == db_val - 1


@pytest.mark.parametrize("method", ["commit", "rollback"])
async def test_conn_begin_await(engine, method, db_val, get_db_val_sql, set_db_val_sql):
    async with engine.connect() as conn:
        tx = await conn.begin()
        await conn.execute(set_db_val_sql, dict(value=db_val + 1))
        await tx.rollback()

        tx = await conn.begin()
        await conn.execute(set_db_val_sql.bindparams(value=db_val - 1))
        await tx.commit()

        tx = await conn.begin()

        with pytest.raises(InvalidRequestError, match="already started"):
            async with tx:
                pass

        rows = await conn.execute(get_db_val_sql).all()

        await getattr(tx, method)()

    assert rows[0][0] == db_val - 1


@pytest.mark.parametrize("method", ["commit", "rollback"])
async def test_conn_begin_not_started(engine, method, db_val, get_db_val_sql):
    async with engine.connect() as conn:
        tx = conn.begin()

        with pytest.raises(InvalidRequestError, match="not started"):
            await tx.commit()
        with pytest.raises(InvalidRequestError, match="not started"):
            await tx.rollback()

        await tx

        with pytest.raises(InvalidRequestError, match="already started"):
            async with tx:
                pass

        rows = await conn.execute(get_db_val_sql).all()

        await getattr(tx, method)()

    assert rows[0][0] == db_val


async def test_not_executable(engine):
    async with engine.connect() as conn:
        with pytest.raises(ObjectNotExecutableError):
            await conn.execute(object())


async def test_begin_failed(engine, mocker):
    mocker.patch("gino.engine.AsyncConnection.begin").side_effect = Exception("BEGIN")
    mocked_close = mocker.MagicMock(wraps=AsyncConnection.close)
    mocker.patch("gino.engine.AsyncConnection.close", lambda conn: mocked_close(conn))

    with pytest.raises(Exception, match="BEGIN"):
        async with engine.begin():
            pass

    mocked_close.assert_called_once()


async def test_dbapi_error(url: URL, conn: AsyncConnection):
    with pytest.raises(DBAPIError):
        await conn.execute(text("SELECT * FROM non_exist;"))

    await conn.execute(text("SELECT 123"))
    with pytest.raises(DBAPIError):
        if url.drivername == "postgresql":
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pg_stat_activity.pid) "
                    "FROM pg_stat_activity WHERE  pid = pg_backend_pid()"
                )
            )
        else:
            await conn.execute(text("KILL connection_id()"))
    with pytest.raises(DBAPIError):
        await conn.execute(text("SELECT 123"))


async def test_connection_closed(url: URL, conn: AsyncConnection, mocker, use_trio):
    if use_trio:
        from trio_mysql import OperationalError
    else:
        from aiomysql import OperationalError
    if url.drivername == "postgresql":
        mocker.patch(
            "gino.dialects.cursor.AsyncCursor.execute"
        ).side_effect = ConnectionDoesNotExistError()
    else:
        mocker.patch(
            "gino.dialects.cursor.AsyncCursor.execute"
        ).side_effect = OperationalError(2014, "Command Out of Sync")
    with pytest.raises(DBAPIError):
        await conn.execute(text("SELECT 123"))
    with pytest.raises(InvalidRequestError):
        await conn.execute(text("SELECT 123"))
