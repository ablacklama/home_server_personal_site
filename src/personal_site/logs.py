from __future__ import annotations

from flask import Blueprint, current_app, render_template, request
from sqlalchemy import func, select

from .activity_log import ActivityLog

bp = Blueprint("logs", __name__, url_prefix="/logs")


@bp.get("/")
def index():
    SessionLocal = current_app.session  # type: ignore[attr-defined]
    if SessionLocal is None:
        return render_template(
            "logs.html",
            title="Activity Logs",
            entries=[],
            categories=[],
            selected_category="",
            search="",
            page=1,
            total_pages=1,
        )

    selected_category = (request.args.get("category") or "").strip()
    search = (request.args.get("q") or "").strip()
    page = max(1, int(request.args.get("page", "1")))
    per_page = 50

    with SessionLocal() as session:
        categories = [
            row[0]
            for row in session.execute(
                select(ActivityLog.category).distinct().order_by(ActivityLog.category)
            ).all()
        ]

        query = select(ActivityLog).order_by(ActivityLog.timestamp.desc())
        count_query = select(func.count(ActivityLog.id))

        if selected_category:
            query = query.where(ActivityLog.category == selected_category)
            count_query = count_query.where(ActivityLog.category == selected_category)
        if search:
            like = f"%{search}%"
            query = query.where(
                ActivityLog.action.ilike(like)
                | ActivityLog.detail.ilike(like)
                | ActivityLog.path.ilike(like)
            )
            count_query = count_query.where(
                ActivityLog.action.ilike(like)
                | ActivityLog.detail.ilike(like)
                | ActivityLog.path.ilike(like)
            )

        total = session.scalar(count_query) or 0
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)

        entries = list(
            session.scalars(query.offset((page - 1) * per_page).limit(per_page)).all()
        )

    return render_template(
        "logs.html",
        title="Activity Logs",
        entries=entries,
        categories=categories,
        selected_category=selected_category,
        search=search,
        page=page,
        total_pages=total_pages,
    )
