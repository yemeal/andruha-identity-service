from datetime import datetime
import uuid

from sqlalchemy import and_, delete, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.application.ports.dto.outbox import OutboxKind, OutboxMessage, OutboxStatus
from app.application.ports.events.base import IntegrationEventProtocol
from app.application.ports.repositories.outbox import OutboxRepositoryProtocol
from app.infrastructure.database.models import OutboxMessageORM
from app.infrastructure.database.repositories.base_repository import (
    SQLAlchemyAsyncRepository,
)


class OutboxRepository(
    SQLAlchemyAsyncRepository[OutboxMessage, OutboxMessageORM, uuid.UUID],
    OutboxRepositoryProtocol,
):
    def __init__(self, session: AsyncSession, topic: str, producer: str) -> None:
        super().__init__(session, OutboxMessage, OutboxMessageORM)
        self._topic = topic
        self._producer = producer

    async def publish(self, event: IntegrationEventProtocol) -> None:
        orm_model = OutboxMessageORM(
            id=event.event_id,
            kind=OutboxKind.EVENT,
            topic=self._topic,
            key=event.partition_key,
            type=event.event_type,
            payload=event.to_envelope_dict(producer=self._producer),
            status=OutboxStatus.PENDING,
            attempts=0,
            available_at=event.occurred_at,
            created_at=event.occurred_at,
            redrive_count=0,
        )
        self._session.add(orm_model)

    async def claim_batch(
        self,
        *,
        owner_token: uuid.UUID,
        claimed_at: datetime,
        claim_expires_at: datetime,
        limit: int,
    ) -> list[OutboxMessage]:
        """
        Атомарный захват батча сообщений.



        :param owner_token: Токен конкретного воркера
        :param claimed_at: Текущее время
        :param claim_expires_at: Время истечения "аренды"
        :param limit: Размер батча
        :return: Список из outbox сообщений
        """
        # Валидируем входные данные
        if limit <= 0:
            raise ValueError("claim limit must be positive")
        if claim_expires_at <= claimed_at:
            raise ValueError("claim expiration must be after claim time")

        # Нам нужно сравнивать записи в таблице outbox друг с другом,
        # поэтому создаем два виртуальных псевдонима одной и той же таблицы:
        # candidate (кандидат на отправку) и earlier (более ранняя запись).
        candidate = aliased(OutboxMessageORM, name="candidate_outbox")
        earlier = aliased(OutboxMessageORM, name="earlier_outbox")

        # что считается «более ранней записью»? Та, у которой created_at меньше.
        # А если время совпало до микросекунды - сравниваем по id
        earlier_position = or_(
            earlier.created_at < candidate.created_at,
            and_(
                earlier.created_at == candidate.created_at,
                earlier.id < candidate.id,
            ),
        )
        # Критически важная защита: если для одного и того же пользователя (key)
        # есть более раннее незавершенное сообщение, мы НЕ имеем права брать новое!
        # Иначе события придут задом наперед.
        has_earlier_nonterminal_for_key = exists(
            select(earlier.id).where(
                earlier.key == candidate.key,
                earlier.status.in_(
                    (OutboxStatus.PENDING, OutboxStatus.CLAIMED),
                ),
                earlier_position,
            )
        )
        # Либо новая запись в статусе `PENDING`, у которой наступило время `available_at`.
        # Либо запись в статусе `CLAIMED`, у которой истекла аренда (`claim_expires_at` <= now).
        # Если другой воркер упал посреди отправки, через 30 секунд его запись подберет живой воркер!
        eligible = or_(
            and_(
                candidate.status == OutboxStatus.PENDING,
                candidate.available_at <= claimed_at,
            ),
            and_(
                candidate.status == OutboxStatus.CLAIMED,
                candidate.claim_expires_at <= claimed_at,
            ),
        )

        # блокировка FOR UPDATE SKIP LOCKED + CTE залоченной выборки
        claimable_ids = (
            select(candidate.id)
            .where(
                eligible,
                ~has_earlier_nonterminal_for_key,
            )
            .order_by(candidate.created_at.asc(), candidate.id.asc())
            .limit(limit)
            .with_for_update(of=candidate, skip_locked=True)
            .cte(
                "claimable_outbox"
            )  # оформляет выборку как временную таблицу в памяти запроса (CTE).
        )

        # атомарный перевод в статус CLAIMED
        statement = (
            update(OutboxMessageORM)
            .where(
                OutboxMessageORM.id.in_(select(claimable_ids.columns.id)),
            )
            .values(
                status=OutboxStatus.CLAIMED,
                claim_token=owner_token,
                claim_expires_at=claim_expires_at,
                terminal_at=None,
            )
            .returning(OutboxMessageORM)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(statement)
        rows = list(result.scalars().all())
        # отвязывает объекты от сессии SQLAlchemy, чтобы их можно было безопасно передавать по приложению.
        self._session.expunge_all()
        rows.sort(key=lambda row: (row.created_at, row.id))
        return [self._to_domain(row) for row in rows]

    async def finalize_published(
        self,
        message_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        published_at: datetime,
    ) -> bool:
        """
        Подтверждение успешной отправки в брокер.

        :param message_id: ID успешно отправленного события
        :param owner_token: Токен воркера, пытающийся финализировать отправку (для защиты от зомби-воркеров)
        :param published_at: timestamp терминализации
        :return: `True` если получилось финализровать, иначе `False`
        """
        statement = (
            update(OutboxMessageORM)
            .where(
                OutboxMessageORM.id == message_id,
                OutboxMessageORM.status == OutboxStatus.CLAIMED,
                # Если воркер завис надолго, то его аренда истекла, и запись перехватит другой воркер.
                # Когда первый очнется и попытается пометить запись как SUCCESS,
                # условие не сойдется, запрос вернет 0 строк, и зомби-воркер ничего не испортит!
                OutboxMessageORM.claim_token == owner_token,
            )
            .values(
                status=OutboxStatus.SUCCESS,
                attempts=OutboxMessageORM.attempts + 1,
                last_error_class=None,
                claim_token=None,
                claim_expires_at=None,
                terminal_at=published_at,
            )
            .returning(OutboxMessageORM.id)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def schedule_retry(
        self,
        message_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        available_at: datetime,
        error_class: str = "TransientPublishError",
    ) -> bool:
        """
        Планирование повторной попытки (Backoff)

        Переводит запись обратно в PENDING, но сдвигает available_at в будущее (экспоненциальный бэкофф).
        """
        statement = (
            update(OutboxMessageORM)
            .where(
                OutboxMessageORM.id == message_id,
                OutboxMessageORM.status == OutboxStatus.CLAIMED,
                OutboxMessageORM.claim_token == owner_token,
            )
            .values(
                status=OutboxStatus.PENDING,
                attempts=OutboxMessageORM.attempts + 1,
                last_error_class=error_class,
                available_at=available_at,
                claim_token=None,
                claim_expires_at=None,
                terminal_at=None,
            )
            .returning(OutboxMessageORM.id)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def quarantine(
        self,
        message_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        quarantined_at: datetime,
        error_class: str = "PermanentOutboxPublishError",
    ) -> bool:
        """
        Отправка в карантин (DLQ)

        Если сообщение битое (схема невалидна)
        или исчерпан лимит попыток (например, 10 раз подряд), оно уходит в QUARANTINED и больше не блокирует очередь.
        """
        statement = (
            update(OutboxMessageORM)
            .where(
                OutboxMessageORM.id == message_id,
                OutboxMessageORM.status == OutboxStatus.CLAIMED,
                OutboxMessageORM.claim_token == owner_token,
            )
            .values(
                status=OutboxStatus.QUARANTINED,
                attempts=OutboxMessageORM.attempts + 1,
                last_error_class=error_class,
                claim_token=None,
                claim_expires_at=None,
                terminal_at=quarantined_at,
            )
            .returning(OutboxMessageORM.id)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def redrive_quarantined(
        self,
        message_id: uuid.UUID,
        *,
        available_at: datetime,
    ) -> bool:
        """
        Ручной перезапуск из карантина
        """
        target = aliased(OutboxMessageORM, name="redrive_target")
        later = aliased(OutboxMessageORM, name="later_outbox")
        later_exists = exists(
            select(later.id)
            .select_from(target)
            .join(
                later,
                and_(
                    later.key == target.key,
                    or_(
                        later.created_at > target.created_at,
                        and_(
                            later.created_at == target.created_at,
                            later.id > target.id,
                        ),
                    ),
                ),
            )
            .where(target.id == message_id)
        )
        statement = (
            update(OutboxMessageORM)
            .where(
                OutboxMessageORM.id == message_id,
                OutboxMessageORM.status == OutboxStatus.QUARANTINED,
                ~later_exists,
            )
            .values(
                status=OutboxStatus.PENDING,
                attempts=0,
                last_error=None,
                last_error_class=None,
                available_at=available_at,
                claim_token=None,
                claim_expires_at=None,
                terminal_at=None,
                redrive_count=OutboxMessageORM.redrive_count + 1,
            )
            .returning(OutboxMessageORM.id)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def delete_terminal_before(
        self,
        *,
        terminal_at: datetime,
        limit: int,
    ) -> int:
        """
        Фоновая очистка

        Чтобы таблица outbox не разрасталась до терабайтов,
        фоновый воркер удаляет старые завершенные сообщения маленькими порциями.
        """
        if limit <= 0:
            raise ValueError("cleanup limit must be positive")
        candidates = (
            select(OutboxMessageORM.id)
            .where(
                OutboxMessageORM.status.in_(
                    (OutboxStatus.SUCCESS, OutboxStatus.QUARANTINED),
                ),
                OutboxMessageORM.terminal_at < terminal_at,
            )
            .order_by(
                OutboxMessageORM.terminal_at.asc(),
                OutboxMessageORM.created_at.asc(),
                OutboxMessageORM.id.asc(),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("expired_terminal_outbox")
        )
        statement = (
            delete(OutboxMessageORM)
            .where(
                OutboxMessageORM.id.in_(
                    select(candidates.c.id),
                )
            )
            .returning(OutboxMessageORM.id)
        )
        result = await self._session.execute(statement)
        return len(result.scalars().all())
