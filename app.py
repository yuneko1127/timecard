from __future__ import annotations

import os
import sqlite3
import calendar
import csv
import io
import secrets
import warnings
from html import escape
from urllib.parse import urlsplit, urlunsplit
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from functools import wraps
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor

from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle, Paragraph
from supabase import create_client, Client
from jose import JWTError, jwt

# .env ファイルから環境変数を読み込む
load_dotenv()

JST = ZoneInfo("Asia/Tokyo")
# 勤怠日の区切り: 当日 4:00 〜 翌日 3:59（日本時間）
ATTENDANCE_DAY_START_HOUR = 4
DB_PATH = os.path.join(os.path.dirname(__file__), "timecard.db")
def _normalize_database_url(url: str) -> str:
    """Heroku/一部ホストの postgres:// を psycopg2 が確実に解釈できる形にする。"""
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]

    # Supabase の session pooler は 5432 だと接続上限に達しやすいので、
    # transaction pooler の 6543 を優先する。
    if "pooler.supabase.com" in url:
        parsed = urlsplit(url)
        if parsed.port == 5432:
            netloc = parsed.netloc.rsplit(":", 1)[0] + ":6543"
            url = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    return url


DATABASE_URL = _normalize_database_url(os.environ.get("DATABASE_URL", "").strip())
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
USE_SUPABASE_AUTH = bool(SUPABASE_URL and SUPABASE_KEY)
EVENT_TYPES = {"clock_in", "clock_out", "break_start", "break_end"}
EVENT_LABELS = {
    "clock_in": "出勤",
    "clock_out": "退勤",
    "break_start": "休憩入り",
    "break_end": "休憩終り",
}


def create_app() -> Flask:
    app = Flask(__name__)

    secret_key = os.environ.get("SECRET_KEY", "").strip()
    if not secret_key or secret_key == "dev-secret-key-change-me":
        if DATABASE_URL or os.environ.get("FLASK_ENV") == "production":
            raise RuntimeError(
                "本番環境（DATABASE_URLが設定されている環境等）では SECRET_KEY の設定が必須です。"
                ".env または環境変数に推測不可能な SECRET_KEY を設定してください。"
            )
        secret_key = secrets.token_hex(32)
        warnings.warn(
            "SECRET_KEY が未設定または初期値のため、ランダムな一時キーを生成しました。"
            "セッション永続化のため、.env に SECRET_KEY を設定してください。",
            UserWarning,
            stacklevel=2,
        )
    app.config["SECRET_KEY"] = secret_key
    app.config["DATABASE_URL"] = DATABASE_URL
    app.config["DATABASE_PATH"] = DB_PATH
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    if DATABASE_URL or os.environ.get("FLASK_ENV") == "production":
        app.config["SESSION_COOKIE_SECURE"] = True

    # Supabase クライアント初期化
    if USE_SUPABASE_AUTH:
        app.config["SUPABASE"] = create_client(SUPABASE_URL, SUPABASE_KEY)
    else:
        app.config["SUPABASE"] = None

    def generate_csrf_token() -> str:
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(32)
        return session["csrf_token"]

    @app.context_processor
    def inject_csrf_token() -> dict[str, Any]:
        return {"csrf_token": generate_csrf_token()}

    @app.before_request
    def before_request() -> None:
        g.db = get_db()
        g.user_id = session.get("user_id")

        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            session_token = session.get("csrf_token")
            if not token or not session_token or not secrets.compare_digest(token, session_token):
                abort(400, description="CSRF token missing or invalid")

    @app.teardown_appcontext
    def teardown_db(exception: BaseException | None = None) -> None:
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.route("/")
    def index():
        if USE_SUPABASE_AUTH:
            if session.get("access_token"):
                return redirect(url_for("punch"))
        elif session.get("user_id"):
            return redirect(url_for("punch"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            if USE_SUPABASE_AUTH:
                if session.get("access_token"):
                    return redirect(url_for("punch"))
            elif session.get("user_id"):
                return redirect(url_for("punch"))
        if request.method == "POST":
            email = request.form.get("email", "").strip()
            password = request.form.get("password", "")

            if not USE_SUPABASE_AUTH:
                flash("Supabase Auth が有効ではありません。SUPABASE_URL と SUPABASE_KEY を設定してください。", "error")
                return render_template("login.html", use_supabase_auth=True)

            try:
                supabase: Client = app.config["SUPABASE"]
                response = supabase.auth.sign_in_with_password({"email": email, "password": password})
                user = getattr(response, "user", None)
                session_data = getattr(response, "session", None)
                if not user or not session_data:
                    flash("ログインに失敗しました。", "error")
                    return render_template("login.html", use_supabase_auth=True)

                local_user = g.db.execute(
                    "SELECT id FROM users WHERE supabase_id = ?",
                    (user.id,),
                ).fetchone()
                if not local_user:
                    local_user = g.db.execute(
                        "SELECT id FROM users WHERE login_id = ?",
                        (email,),
                    ).fetchone()

                if not local_user:
                    g.db.execute(
                        "INSERT INTO users (login_id, supabase_id) VALUES (?, ?)",
                        (email, user.id),
                    )
                    g.db.commit()
                    local_user = g.db.execute(
                        "SELECT id FROM users WHERE supabase_id = ?",
                        (user.id,),
                    ).fetchone()

                if not local_user:
                    flash("ローカルユーザーの作成に失敗しました。", "error")
                    return render_template("login.html", use_supabase_auth=True)

                g.db.execute(
                    "UPDATE users SET supabase_id = ? WHERE id = ?",
                    (user.id, local_user["id"]),
                )
                g.db.commit()
                session.clear()
                session["access_token"] = session_data.access_token
                session["refresh_token"] = session_data.refresh_token
                session["user_id"] = local_user["id"]
                session["login_id"] = email
                session["supabase_user_id"] = user.id
                session["user_email"] = getattr(user, "email", email)
                session["last_validated_at"] = datetime.now(timezone.utc).timestamp()
                session["csrf_token"] = secrets.token_hex(32)
                return redirect(url_for("punch"))
            except Exception as exc:
                flash(f"ログインに失敗しました: {exc}", "error")

        return render_template("login.html", use_supabase_auth=USE_SUPABASE_AUTH)

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        if USE_SUPABASE_AUTH:
            try:
                supabase: Client = app.config["SUPABASE"]
                supabase.auth.sign_out()
            except Exception:
                pass
        session.clear()
        flash("ログアウトしました。", "info")
        return redirect(url_for("login"))

    @app.route("/punch", methods=["GET", "POST"])
    @login_required
    def punch():
        active_companies = g.db.execute(
            "SELECT id, name FROM companies WHERE is_active = 1 ORDER BY id"
        ).fetchall()
        warning = None

        if request.method == "POST":
            company_id = request.form.get("company_id")
            event_type = request.form.get("event_type")
            user_id = session["user_id"]

            if event_type not in EVENT_TYPES:
                flash("不正な打刻種別です。", "error")
                return redirect(url_for("punch"))
            if not company_id:
                flash("会社を選択してください。", "error")
                return redirect(url_for("punch"))

            now_dt = datetime.now(JST)
            warning = validate_event_order(g.db, user_id, int(company_id), event_type, now_dt)
            g.db.execute(
                """
                INSERT INTO time_events (user_id, company_id, event_type, event_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, company_id, event_type, now_dt.isoformat()),
            )
            g.db.commit()

            if warning:
                flash(f"打刻は保存しました（警告: {warning}）。", "warning")
            else:
                flash(f"{EVENT_LABELS[event_type]}を記録しました。", "success")
            return redirect(url_for("punch"))

        latest_events = g.db.execute(
            """
            SELECT te.event_type, te.event_at, c.name AS company_name
            FROM time_events te
            JOIN companies c ON c.id = te.company_id AND c.is_active = 1
            WHERE te.user_id = ?
            ORDER BY te.event_at DESC
            LIMIT 10
            """,
            (session["user_id"],),
        ).fetchall()

        # 会社ごとの最新イベントから勤務状態を判定する
        company_states: dict[str, dict[str, str]] = {}
        all_latest_events = g.db.execute(
            """
            SELECT company_id, event_type
            FROM (
                SELECT
                    te.company_id,
                    te.event_type,
                    ROW_NUMBER() OVER (
                        PARTITION BY te.company_id
                        ORDER BY te.event_at DESC, te.id DESC
                    ) AS rn
                FROM time_events te
                WHERE te.user_id = ?
            ) AS latest
            WHERE latest.rn = 1
            ORDER BY company_id ASC
            """,
            (session["user_id"],),
        ).fetchall()
        for r in all_latest_events:
            cid = str(r["company_id"])
            event_type = r["event_type"]
            if event_type == "break_start":
                company_states[cid] = {"kind": "break", "label": "休憩中"}
            elif event_type == "clock_out":
                company_states[cid] = {"kind": "off", "label": "勤務外"}
            else:
                # clock_in または break_end（または想定外の値）=> 勤務中として扱う
                company_states[cid] = {"kind": "work", "label": "勤務中"}

        return render_template(
            "punch.html",
            companies=active_companies,
            events=latest_events,
            labels=EVENT_LABELS,
            company_states=company_states,
        )

    @app.route("/attendance")
    @login_required
    def attendance():
        month = request.args.get("month")
        company_id = request.args.get("company_id", "all")
        now = datetime.now(JST)
        today_key = attendance_day_key(now)
        if not month:
            month = today_key[:7]
        try:
            target_month = datetime.strptime(month, "%Y-%m")
        except ValueError:
            flash("月指定が不正です。YYYY-MM 形式で指定してください。", "error")
            return redirect(url_for("attendance"))

        companies = g.db.execute(
            "SELECT id, name FROM companies WHERE is_active = 1 ORDER BY id"
        ).fetchall()
        company_map = {str(c["id"]): c["name"] for c in companies}
        selected_company_name = "全会社"
        if company_id != "all":
            if company_id not in company_map:
                flash("指定された会社が存在しません。", "error")
                return redirect(url_for("attendance", month=month, company_id="all"))
            selected_company_name = company_map[company_id]

        summary = build_attendance_summary(
            g.db,
            session["user_id"],
            target_month,
            company_id,
            selected_company_name,
        )
        company_totals, grand_total_minutes = calculate_company_totals(summary)
        prev_month, next_month = month_prev_next(month)

        return render_template(
            "attendance.html",
            month=month,
            prev_month=prev_month,
            next_month=next_month,
            summary=summary,
            companies=companies,
            selected_company_id=company_id,
            company_totals=company_totals,
            grand_total_minutes=grand_total_minutes,
            today_key=today_key,
        )

    @app.route("/attendance/day/<day_key>", methods=["GET", "POST"])
    @login_required
    def attendance_day_edit(day_key: str):
        try:
            datetime.strptime(day_key, "%Y-%m-%d")
        except ValueError:
            flash("日付が不正です。YYYY-MM-DD 形式で指定してください。", "error")
            return redirect(url_for("attendance"))

        return_month = request.args.get("month") or day_key[:7]
        user_id = session["user_id"]

        if request.method == "POST":
            action = request.form.get("action")
            try:
                if action == "update":
                    event_id = int(request.form.get("event_id", "0"))
                    company_id = int(request.form.get("company_id", "0"))
                    active_company = g.db.execute(
                        "SELECT id FROM companies WHERE id = ? AND is_active = 1",
                        (company_id,),
                    ).fetchone()
                    if not active_company:
                        flash("選択された会社は無効か存在しません。", "error")
                        return redirect(url_for("attendance_day_edit", day_key=day_key, month=return_month))
                    event_type = request.form.get("event_type", "")
                    event_time_raw = request.form.get("event_time", "")
                    if event_type not in EVENT_TYPES:
                        flash("不正な打刻種別です。", "error")
                        return redirect(url_for("attendance_day_edit", day_key=day_key, month=return_month))
                    event_at = combine_attendance_day_and_time(day_key, event_time_raw)
                    assert_event_in_attendance_day(event_at, day_key)
                    row = g.db.execute(
                        "SELECT id FROM time_events WHERE id = ? AND user_id = ?",
                        (event_id, user_id),
                    ).fetchone()
                    if not row:
                        flash("対象の打刻が見つかりません。", "error")
                        return redirect(url_for("attendance_day_edit", day_key=day_key, month=return_month))
                    g.db.execute(
                        """
                        UPDATE time_events
                        SET company_id = ?, event_type = ?, event_at = ?
                        WHERE id = ? AND user_id = ?
                        """,
                        (company_id, event_type, event_at.isoformat(), event_id, user_id),
                    )
                    g.db.commit()
                    flash("打刻を更新しました。", "success")
                elif action == "delete":
                    event_id = int(request.form.get("event_id", "0"))
                    cur = g.db.execute(
                        "DELETE FROM time_events WHERE id = ? AND user_id = ?",
                        (event_id, user_id),
                    )
                    g.db.commit()
                    if cur.rowcount:
                        flash("打刻を削除しました。", "success")
                    else:
                        flash("削除対象の打刻が見つかりません。", "error")
                elif action == "add":
                    company_id = int(request.form.get("company_id", "0"))
                    active_company = g.db.execute(
                        "SELECT id FROM companies WHERE id = ? AND is_active = 1",
                        (company_id,),
                    ).fetchone()
                    if not active_company:
                        flash("選択された会社は無効か存在しません。", "error")
                        return redirect(url_for("attendance_day_edit", day_key=day_key, month=return_month))
                    event_type = request.form.get("event_type", "")
                    event_time_raw = request.form.get("event_time", "")
                    if event_type not in EVENT_TYPES:
                        flash("不正な打刻種別です。", "error")
                        return redirect(url_for("attendance_day_edit", day_key=day_key, month=return_month))
                    event_at = combine_attendance_day_and_time(day_key, event_time_raw)
                    assert_event_in_attendance_day(event_at, day_key)
                    g.db.execute(
                        """
                        INSERT INTO time_events (user_id, company_id, event_type, event_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (user_id, company_id, event_type, event_at.isoformat()),
                    )
                    g.db.commit()
                    flash("打刻を追加しました。", "success")
                else:
                    flash("不正な操作です。", "error")
            except ValueError as e:
                flash(str(e), "error")
            return redirect(url_for("attendance_day_edit", day_key=day_key, month=return_month))

        day_start, day_end_exclusive = attendance_day_bounds(day_key)
        rows = g.db.execute(
            """
            SELECT te.id, te.company_id, te.event_type, te.event_at, c.name AS company_name
            FROM time_events te
            JOIN companies c ON c.id = te.company_id
            WHERE te.user_id = ? AND te.event_at >= ? AND te.event_at < ?
            ORDER BY te.event_at ASC, te.id ASC
            """,
            (user_id, day_start.isoformat(), day_end_exclusive.isoformat()),
        ).fetchall()

        events = [
            {
                "id": r["id"],
                "company_id": r["company_id"],
                "event_type": r["event_type"],
                "event_at": r["event_at"],
                "company_name": r["company_name"],
                "event_time": datetime.fromisoformat(r["event_at"])
                .astimezone(JST)
                .strftime("%H:%M"),
            }
            for r in rows
        ]

        companies = g.db.execute(
            "SELECT id, name FROM companies WHERE is_active = 1 ORDER BY id"
        ).fetchall()
        default_add_time = "12:00"
        day_warning_messages = collect_day_warning_messages(list(rows))
        return render_template(
            "attendance_day_edit.html",
            day_key=day_key,
            return_month=return_month,
            events=events,
            companies=companies,
            labels=EVENT_LABELS,
            default_add_time=default_add_time,
            day_warning_messages=day_warning_messages,
        )

    @app.route("/attendance/csv")
    @login_required
    def attendance_csv():
        month = request.args.get("month", datetime.now(JST).strftime("%Y-%m"))
        company_id = request.args.get("company_id", "all")
        try:
            target_month = datetime.strptime(month, "%Y-%m")
        except ValueError:
            flash("月指定が不正です。YYYY-MM 形式で指定してください。", "error")
            return redirect(url_for("attendance"))

        companies = g.db.execute("SELECT id, name FROM companies ORDER BY id").fetchall()
        company_map = {str(c["id"]): c["name"] for c in companies}
        selected_company_name = "全会社"
        if company_id != "all":
            if company_id not in company_map:
                flash("指定された会社が存在しません。", "error")
                return redirect(url_for("attendance", month=month, company_id="all"))
            selected_company_name = company_map[company_id]

        summary = build_attendance_summary(
            g.db,
            session["user_id"],
            target_month,
            company_id,
            selected_company_name,
        )

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["日付", "会社", "出勤", "退勤", "休憩入り", "休憩終り", "休憩(分)", "勤務(分)", "実働(分)", "警告"])
        for row in summary:
            writer.writerow(
                [
                    row["date"],
                    row["company_name"],
                    row["clock_in"],
                    row["clock_out"],
                    row["break_start"],
                    row["break_end"],
                    row["break_minutes"],
                    row["work_minutes"],
                    row["net_minutes"],
                    warning_cell_export(row["warning"]),
                ]
            )

        response = make_response(output.getvalue())
        response.headers["Content-Type"] = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = f"attachment; filename=attendance_{month}_{company_id}.csv"
        return response

    @app.route("/attendance/pdf")
    @login_required
    def attendance_pdf():
        month = request.args.get("month", datetime.now(JST).strftime("%Y-%m"))
        company_id = request.args.get("company_id", "all")
        try:
            target_month = datetime.strptime(month, "%Y-%m")
        except ValueError:
            flash("月指定が不正です。YYYY-MM 形式で指定してください。", "error")
            return redirect(url_for("attendance"))

        companies = g.db.execute("SELECT id, name FROM companies ORDER BY id").fetchall()
        company_map = {str(c["id"]): c["name"] for c in companies}
        selected_company_name = "全会社"
        if company_id != "all":
            if company_id not in company_map:
                flash("指定された会社が存在しません。", "error")
                return redirect(url_for("attendance", month=month, company_id="all"))
            selected_company_name = company_map[company_id]

        summary = build_attendance_summary(
            g.db,
            session["user_id"],
            target_month,
            company_id,
            selected_company_name,
        )

        buffer = io.BytesIO()
        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
        doc = SimpleDocTemplate(buffer, pagesize=landscape(A4))
        styles = getSampleStyleSheet()
        styles["Normal"].fontName = "HeiseiKakuGo-W5"
        styles["Title"].fontName = "HeiseiKakuGo-W5"

        data = [["日付", "会社", "出勤", "退勤", "休憩入り", "休憩終り", "休憩(分)", "勤務(分)", "実働(分)", "警告"]]
        for row in summary:
            data.append(
                [
                    row["date"],
                    row["company_name"],
                    row["clock_in"],
                    row["clock_out"],
                    row["break_start"],
                    row["break_end"],
                    str(row["break_minutes"]),
                    str(row["work_minutes"]),
                    str(row["net_minutes"]),
                    warning_cell_export(row["warning"]),
                ]
            )
        table = Table(data, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), "HeiseiKakuGo-W5"),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ]
            )
        )

        title = Paragraph(f"勤怠確認 {month} / {escape(selected_company_name)}", styles["Title"])
        doc.build([title, Spacer(1, 12), table])
        pdf_bytes = buffer.getvalue()
        buffer.close()

        response = make_response(pdf_bytes)
        response.headers["Content-Type"] = "application/pdf"
        response.headers["Content-Disposition"] = f"attachment; filename=attendance_{month}_{company_id}.pdf"
        return response

    @app.route("/companies", methods=["GET", "POST"])
    @login_required
    def companies():
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                name = request.form.get("name", "").strip()
                if not name:
                    flash("会社名を入力してください。", "error")
                else:
                    g.db.execute(
                        "INSERT INTO companies (name, is_active) VALUES (?, 1)",
                        (name,),
                    )
                    g.db.commit()
                    flash("会社を追加しました。", "success")
            elif action == "update_all":
                rows = g.db.execute("SELECT id FROM companies ORDER BY id").fetchall()
                updates: list[tuple[str, int, int]] = []
                for row in rows:
                    cid = int(row["id"])
                    name = request.form.get(f"name_{cid}", "").strip()
                    is_active = 1 if request.form.get(f"active_{cid}") == "on" else 0
                    if not name:
                        flash("会社名が空の行があります。すべての会社名を入力してください。", "error")
                        return redirect(url_for("companies"))
                    updates.append((name, is_active, cid))
                for name, is_active, cid in updates:
                    g.db.execute(
                        "UPDATE companies SET name = ?, is_active = ? WHERE id = ?",
                        (name, is_active, cid),
                    )
                g.db.commit()
                flash("会社情報を更新しました。", "success")
            return redirect(url_for("companies"))

        items = g.db.execute("SELECT id, name, is_active FROM companies ORDER BY id").fetchall()
        return render_template("companies.html", companies=items)

    init_db(app)
    return app


def ensure_valid_supabase_session() -> bool:
    access_token = session.get("access_token")
    if not access_token:
        return False

    now = datetime.now(timezone.utc).timestamp()
    try:
        claims = jwt.get_unverified_claims(access_token)
        exp = claims.get("exp", 0)
    except Exception:
        session.clear()
        flash("セッション情報が無効です。再度ログインしてください。", "error")
        return False

    supabase: Client | None = current_app.config.get("SUPABASE")
    if not supabase:
        return True

    # 有効期限切れ（または残り60秒以内）の場合はリフレッシュを試みる
    if exp <= now + 60:
        refresh_token = session.get("refresh_token")
        if not refresh_token:
            session.clear()
            flash("セッションの有効期限が切れました。再度ログインしてください。", "info")
            return False
        try:
            res = supabase.auth.refresh_session(refresh_token)
            new_session = getattr(res, "session", None)
            if not new_session:
                session.clear()
                flash("セッションの更新に失敗しました。再度ログインしてください。", "info")
                return False
            session["access_token"] = new_session.access_token
            session["refresh_token"] = new_session.refresh_token
            session["last_validated_at"] = now
            return True
        except Exception:
            session.clear()
            flash("セッションの有効期限が切れました。再度ログインしてください。", "info")
            return False

    # 5分ごとにSupabaseサーバー側でユーザーの有効性を確認
    last_val = session.get("last_validated_at", 0)
    if now - last_val > 300:
        try:
            user_resp = supabase.auth.get_user(jwt=access_token)
            if not user_resp or not getattr(user_resp, "user", None):
                session.clear()
                flash("アカウントの認証状態が無効化されました。再度ログインしてください。", "error")
                return False
            session["last_validated_at"] = now
        except Exception:
            # ネットワーク接続障害等の一時エラー時は、ローカルJWTのexpが有効であれば通過
            pass

    return True


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        # Supabase Auth を使用している場合
        if USE_SUPABASE_AUTH:
            if not session.get("access_token"):
                return redirect(url_for("login"))
            if not ensure_valid_supabase_session():
                return redirect(url_for("login"))
        else:
            # 従来のセッション認証
            if not session.get("user_id"):
                return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def _sql_placeholders(sql: str) -> str:
    """SQLite の ? を psycopg2 の %s に置き換える。"""
    return sql.replace("?", "%s")


class PgCursor:
    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def fetchone(self) -> Any:
        return self._raw.fetchone()

    def fetchall(self) -> list[Any]:
        return self._raw.fetchall()

    @property
    def rowcount(self) -> int:
        return self._raw.rowcount


class PgConnection:
    """psycopg2 を sqlite3 と同様の execute().fetchall() チェーンで使う。"""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] | None = None) -> PgCursor:
        cur = self._conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(_sql_placeholders(sql), params or ())
        return PgCursor(cur)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def get_db() -> Any:
    if "db" not in g:
        if DATABASE_URL:
            raw = psycopg2.connect(DATABASE_URL)
            g.db = PgConnection(raw)
        else:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            g.db = conn
    return g.db


def init_db(app: Flask) -> None:
    if DATABASE_URL:
        _init_db_postgres()
    else:
        _init_db_sqlite(app.config["DATABASE_PATH"])


def _ensure_supabase_id_column_sqlite(conn: sqlite3.Connection) -> None:
    columns = conn.execute("PRAGMA table_info(users)").fetchall()
    column_names = {row[1] for row in columns}
    if "supabase_id" not in column_names:
        conn.execute("ALTER TABLE users ADD COLUMN supabase_id TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_supabase_id ON users(supabase_id)")


def _ensure_supabase_id_column_postgres(raw: Any) -> None:
    cur = raw.cursor()
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'supabase_id'
        LIMIT 1
        """
    )
    if cur.fetchone() is None:
        cur.execute("ALTER TABLE users ADD COLUMN supabase_id TEXT")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_supabase_id ON users(supabase_id)")
    raw.commit()
    cur.close()


def _init_db_sqlite(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login_id TEXT NOT NULL UNIQUE,
            password_hash TEXT,
            supabase_id TEXT UNIQUE
        );

        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS time_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            company_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            event_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(company_id) REFERENCES companies(id)
        );
        """
    )
    _ensure_supabase_id_column_sqlite(conn)
    # 初期ユーザーは環境変数で指定された場合のみ作成
    company = conn.execute("SELECT id FROM companies LIMIT 1").fetchone()
    if not company:
        conn.execute("INSERT INTO companies (name, is_active) VALUES (?, 1)", ("デフォルト会社",))
    conn.commit()
    conn.close()


def _init_db_postgres() -> None:
    raw = psycopg2.connect(DATABASE_URL)
    cur = raw.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            login_id TEXT NOT NULL UNIQUE,
            password_hash TEXT,
            supabase_id TEXT UNIQUE
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS companies (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS time_events (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            company_id INTEGER NOT NULL REFERENCES companies(id),
            event_type TEXT NOT NULL,
            event_at TEXT NOT NULL
        )
        """
    )
    raw.commit()
    cur.close()

    _ensure_supabase_id_column_postgres(raw)

    db = PgConnection(raw)
    # 初期ユーザーは環境変数で指定された場合のみ作成
    company = db.execute("SELECT id FROM companies LIMIT 1").fetchone()
    if not company:
        db.execute("INSERT INTO companies (name, is_active) VALUES (?, 1)", ("デフォルト会社",))
        db.commit()
    db.close()


@dataclass
class ShiftEntry:
    company_name: str
    clock_in: str
    clock_out: str
    break_start: str
    break_end: str
    break_minutes: int
    work_minutes: int
    net_minutes: int
    warning: str | None


def month_prev_next(month_key: str) -> tuple[str, str]:
    y = int(month_key[:4])
    mo = int(month_key[5:7])
    if mo == 1:
        prev_y, prev_m = y - 1, 12
    else:
        prev_y, prev_m = y, mo - 1
    if mo == 12:
        next_y, next_m = y + 1, 1
    else:
        next_y, next_m = y, mo + 1
    return f"{prev_y:04d}-{prev_m:02d}", f"{next_y:04d}-{next_m:02d}"


def format_dt(iso_dt: str | None) -> str:
    if not iso_dt:
        return "-"
    return datetime.fromisoformat(iso_dt).astimezone(JST).strftime("%Y-%m-%d %H:%M")


def to_minutes_text(minutes: int) -> str:
    h = minutes // 60
    m = minutes % 60
    return f"{h}時間{m}分"


def build_attendance_summary(
    conn: Any,
    user_id: int,
    target_month: datetime,
    company_id: str,
    selected_company_name: str,
) -> list[dict[str, str | int | None]]:
    y, m = target_month.year, target_month.month
    first = datetime(y, m, 1, tzinfo=JST).date()
    if m == 12:
        next_first = datetime(y + 1, 1, 1, tzinfo=JST).date()
    else:
        next_first = datetime(y, m + 1, 1, tzinfo=JST).date()
    query_start = datetime.combine(first, time(ATTENDANCE_DAY_START_HOUR, 0, 0), tzinfo=JST)
    query_end = datetime.combine(next_first, time(ATTENDANCE_DAY_START_HOUR, 0, 0), tzinfo=JST)

    query = """
        SELECT te.company_id, te.event_type, te.event_at, c.name AS company_name
        FROM time_events te
        JOIN companies c ON c.id = te.company_id AND c.is_active = 1
        WHERE te.user_id = ? AND te.event_at >= ? AND te.event_at < ?
    """
    params: list[str | int] = [user_id, query_start.isoformat(), query_end.isoformat()]
    if company_id != "all":
        query += " AND te.company_id = ?"
        params.append(int(company_id))
    query += " ORDER BY te.event_at ASC"

    rows = conn.execute(query, tuple(params)).fetchall()

    grouped: dict[str, list[Any]] = defaultdict(list)
    for r in rows:
        dt = datetime.fromisoformat(r["event_at"]).astimezone(JST)
        grouped[attendance_day_key(dt)].append(r)

    days_in_month = calendar.monthrange(target_month.year, target_month.month)[1]
    summary: list[dict[str, str | int | None]] = []
    for day in range(1, days_in_month + 1):
        day_date = target_month.replace(day=day)
        day_key = day_date.strftime("%Y-%m-%d")
        day_rows = grouped.get(day_key, [])
        if not day_rows:
            summary.append(
                {
                    "date": day_key,
                    "day_key": day_key,
                    "company_name": "",
                    "clock_in": "-",
                    "clock_out": "-",
                    "break_start": "-",
                    "break_end": "-",
                    "break_minutes": 0,
                    "work_minutes": 0,
                    "net_minutes": 0,
                    "warning": None,
                    "has_data": False,
                }
            )
            continue

        rows_by_company: dict[int, list[Any]] = defaultdict(list)
        for row in day_rows:
            rows_by_company[int(row["company_id"])].append(row)

        ordered_company_ids = sorted(
            rows_by_company.keys(),
            key=lambda cid: (
                datetime.fromisoformat(rows_by_company[cid][0]["event_at"]),
                cid,
            ),
        )

        day_output_rows: list[dict[str, str | int | None]] = []
        for cid in ordered_company_ids:
            company_rows = rows_by_company[cid]
            shifts, day_level_warnings = calculate_day_shifts(company_rows)
            for idx, shift in enumerate(shifts):
                row_warning = shift.warning
                if idx == 0 and day_level_warnings:
                    merged_day = " / ".join(day_level_warnings)
                    row_warning = (
                        f"{row_warning} / {merged_day}" if row_warning else merged_day
                    )
                day_output_rows.append(
                    {
                        "date": "",
                        "day_key": day_key,
                        "company_name": shift.company_name,
                        "clock_in": shift.clock_in,
                        "clock_out": shift.clock_out,
                        "break_start": shift.break_start,
                        "break_end": shift.break_end,
                        "break_minutes": shift.break_minutes,
                        "work_minutes": shift.work_minutes,
                        "net_minutes": shift.net_minutes,
                        "warning": row_warning,
                        "has_data": True,
                    }
                )

        if not day_output_rows:
            summary.append(
                {
                    "date": day_key,
                    "day_key": day_key,
                    "company_name": "",
                    "clock_in": "-",
                    "clock_out": "-",
                    "break_start": "-",
                    "break_end": "-",
                    "break_minutes": 0,
                    "work_minutes": 0,
                    "net_minutes": 0,
                    "warning": None,
                    "has_data": False,
                }
            )
            continue

        for idx, row in enumerate(day_output_rows):
            row["date"] = day_key if idx == 0 else ""
            summary.append(
                row
            )
    return summary


def calculate_company_totals(
    summary: list[dict[str, str | int | None]],
) -> tuple[dict[str, int], int]:
    per_company: dict[str, int] = defaultdict(int)
    for row in summary:
        if not row.get("has_data"):
            continue
        company_name = str(row.get("company_name") or "")
        per_company[company_name] += int(row.get("net_minutes") or 0)
    grand_total = sum(per_company.values())
    return dict(sorted(per_company.items(), key=lambda x: x[0])), grand_total


def validate_event_order(
    conn: Any,
    user_id: int,
    company_id: int,
    event_type: str,
    now_dt: datetime,
) -> str | None:
    day_start, day_end_exclusive = attendance_day_bounds(attendance_day_key(now_dt))
    rows = conn.execute(
        """
        SELECT event_type
        FROM time_events
        WHERE user_id = ? AND company_id = ? AND event_at >= ? AND event_at < ?
        ORDER BY event_at ASC
        """,
        (user_id, company_id, day_start.isoformat(), day_end_exclusive.isoformat()),
    ).fetchall()
    history = [r["event_type"] for r in rows]

    if event_type == "clock_out" and "clock_in" not in history:
        return "出勤前の退勤です"
    if event_type == "break_start" and "clock_in" not in history:
        return "出勤前の休憩入りです"
    if event_type == "break_end" and "break_start" not in history:
        return "休憩入り前の休憩終りです"
    return None


def calculate_day_shifts(rows: list[Any]) -> tuple[list[ShiftEntry], list[str]]:
    shifts: list[ShiftEntry] = []
    current: dict[str, object] | None = None
    day_warnings: list[str] = []

    for row in rows:
        ev = row["event_type"]
        at = row["event_at"]
        company_name = str(row["company_name"]) if "company_name" in row.keys() else ""

        if ev == "clock_in":
            if current is not None:
                day_warnings.append("連続した出勤打刻があります")
                shifts.append(build_shift_entry(current, warning="退勤に対応しない出勤打刻があります"))
            current = {
                "company_name": company_name,
                "clock_in": at,
                "clock_out": None,
                "break_starts": [],
                "break_ends": [],
                "break_minutes": 0,
                "pending_break_start": None,
                "warning": None,
            }
            continue

        if ev == "clock_out":
            if current is None:
                day_warnings.append("出勤前の退勤打刻があります")
                continue
            current["clock_out"] = at
            shifts.append(build_shift_entry(current))
            current = None
            continue

        if ev == "break_start":
            if current is None:
                day_warnings.append("出勤外の休憩入り打刻があります")
                continue
            starts = current["break_starts"]
            assert isinstance(starts, list)
            starts.append(at)
            if current["pending_break_start"] is not None:
                current["warning"] = "連続した休憩入り打刻があります"
            current["pending_break_start"] = at
            continue

        if ev == "break_end":
            if current is None:
                day_warnings.append("出勤外の休憩終り打刻があります")
                continue
            ends = current["break_ends"]
            assert isinstance(ends, list)
            ends.append(at)
            pending = current["pending_break_start"]
            if pending is None:
                current["warning"] = "休憩終りのみが存在"
                continue
            break_end_dt = truncate_to_minute(datetime.fromisoformat(at))
            break_start_dt = truncate_to_minute(datetime.fromisoformat(str(pending)))
            delta = break_end_dt - break_start_dt
            current["break_minutes"] = int(current["break_minutes"]) + max(0, int(delta.total_seconds() // 60))
            current["pending_break_start"] = None

    if current is not None:
        shifts.append(build_shift_entry(current, warning="退勤に対応しない出勤打刻があります"))

    return shifts, day_warnings


def warning_cell_export(warning: str | None) -> str:
    return "警告" if warning else ""


def flatten_warning_messages(warning: str | None) -> list[str]:
    if not warning:
        return []
    return [p.strip() for p in warning.split(" / ") if p.strip()]


def collect_day_warning_messages(rows: list[Any]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []

    rows_by_company: dict[int, list[Any]] = defaultdict(list)
    for row in rows:
        if "company_id" in row.keys():
            rows_by_company[int(row["company_id"])].append(row)
        else:
            rows_by_company[0].append(row)

    for company_rows in rows_by_company.values():
        shifts, day_warnings = calculate_day_shifts(company_rows)
        for msg in day_warnings:
            if msg not in seen:
                seen.add(msg)
                ordered.append(msg)
        for shift in shifts:
            for msg in flatten_warning_messages(shift.warning):
                if msg not in seen:
                    seen.add(msg)
                    ordered.append(msg)
    return ordered


def build_shift_entry(shift: dict[str, object], warning: str | None = None) -> ShiftEntry:
    shift_warning = warning or (str(shift.get("warning")) if shift.get("warning") else None)
    pending = shift.get("pending_break_start")
    if pending is not None:
        if shift_warning:
            shift_warning = f"{shift_warning} / 休憩入りに対応する休憩終りが未登録"
        else:
            shift_warning = "休憩入りに対応する休憩終りが未登録"

    in_iso = str(shift.get("clock_in") or "")
    out_iso = str(shift.get("clock_out") or "")
    work_minutes = 0
    if in_iso and out_iso:
        work_minutes = max(
            0,
            int(
                (
                    truncate_to_minute(datetime.fromisoformat(out_iso))
                    - truncate_to_minute(datetime.fromisoformat(in_iso))
                ).total_seconds()
                // 60
            ),
        )
    break_minutes = int(shift.get("break_minutes") or 0)
    net_minutes = max(0, work_minutes - break_minutes)

    return ShiftEntry(
        company_name=str(shift.get("company_name") or ""),
        clock_in=format_dt_list([in_iso] if in_iso else []),
        clock_out=format_dt_list([out_iso] if out_iso else []),
        break_start=format_dt_list([str(x) for x in shift.get("break_starts", [])]),
        break_end=format_dt_list([str(x) for x in shift.get("break_ends", [])]),
        break_minutes=break_minutes,
        work_minutes=work_minutes,
        net_minutes=net_minutes,
        warning=shift_warning,
    )


def truncate_to_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def attendance_day_key(dt: datetime) -> str:
    dt_jst = dt.astimezone(JST)
    d = dt_jst.date()
    if dt_jst.hour < ATTENDANCE_DAY_START_HOUR:
        d = d - timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def attendance_day_bounds(day_key: str) -> tuple[datetime, datetime]:
    d = datetime.strptime(day_key, "%Y-%m-%d").date()
    start = datetime.combine(d, time(ATTENDANCE_DAY_START_HOUR, 0, 0), tzinfo=JST)
    end_exclusive = start + timedelta(days=1)
    return start, end_exclusive


def combine_attendance_day_and_time(day_key: str, time_str: str) -> datetime:
    time_str = time_str.strip()
    if not time_str:
        raise ValueError("時間を入力してください。")
    parts = time_str.split(":")
    if len(parts) < 2:
        raise ValueError("時間の形式が不正です（HH:MM）。")
    try:
        h = int(parts[0])
        m = int(parts[1])
        s = int(parts[2]) if len(parts) > 2 else 0
    except ValueError as exc:
        raise ValueError("時間の形式が不正です（HH:MM）。") from exc
    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
        raise ValueError("時間が不正です。")
    t = time(hour=h, minute=m, second=s)
    d = datetime.strptime(day_key, "%Y-%m-%d").date()
    if h < ATTENDANCE_DAY_START_HOUR:
        d = d + timedelta(days=1)
    return datetime.combine(d, t, tzinfo=JST)


def assert_event_in_attendance_day(dt: datetime, day_key: str) -> None:
    start, end_excl = attendance_day_bounds(day_key)
    dt_jst = dt.astimezone(JST)
    if not (start <= dt_jst < end_excl):
        raise ValueError(
            "時刻はこの勤怠日の範囲（当日4:00〜翌3:59）内で指定してください。"
        )


def format_dt_list(iso_list: list[str]) -> str:
    if not iso_list:
        return "-"
    formatted = [datetime.fromisoformat(i).astimezone(JST).strftime("%H:%M") for i in iso_list]
    return "\n".join(formatted)


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
