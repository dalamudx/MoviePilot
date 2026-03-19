from sqlalchemy import Column, Integer, String, Boolean, JSON, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db import db_query, Base, get_id_column, async_db_query


class SubscribeProgress(Base):
    id = get_id_column()
    subscribe_id = Column(Integer, nullable=False, index=True, unique=True)
    tmdbid = Column(Integer, index=True)
    doubanid = Column(String, index=True)
    media_type = Column(String)
    season = Column(Integer)
    total_episode = Column(Integer)
    pending_episodes = Column(JSON, default=list)
    downloaded_episodes = Column(JSON, default=list)
    transferred_episodes = Column(JSON, default=list)
    completed = Column(Boolean, default=False, index=True)
    last_download_hash = Column(String, index=True)
    date = Column(String)
    last_update = Column(String)

    @classmethod
    @db_query
    def get_by_subscribe_id(cls, db: Session, subscribe_id: int):
        return db.query(cls).filter(cls.subscribe_id == subscribe_id).first()

    @classmethod
    @async_db_query
    async def async_get_by_subscribe_id(cls, db: AsyncSession, subscribe_id: int):
        result = await db.execute(select(cls).filter(cls.subscribe_id == subscribe_id))
        return result.scalars().first()
