import os
import uuid
from datetime import datetime, date, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    abort,
    send_from_directory,
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    login_required,
    current_user,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import or_, and_, text, inspect
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
TZ = ZoneInfo("Europe/Bucharest")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
database_url = os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'database.db')}")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOAD_DIR
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "webp"}

SECTION_DEFAULTS = [
    ("Sectia de tocuri", 1, 2),
    ("Sectia de usi crude", 2, 3),
    ("Sectia de slefuire / grund / vopsea", 3, 3),
    ("Sectia de canturi / frezare / asamblare", 4, 3),
    ("Sectia de control si impachetare", 5, 1),
    ("Sectia de livrare", 6, 1),
]

TRANSFER_SENT = "trimisa"
TRANSFER_ACCEPTED = "acceptata"
TRANSFER_REJECTED = "refuzata"

ORDER_NEW = "noua"
ORDER_IN_PROGRESS = "in_lucru"
ORDER_AWAITING_RECEIPT = "in_asteptare_receptie"
ORDER_REJECTED = "refuzata_pentru_remedieri"
ORDER_COMPLETED = "finalizata"
ORDER_DELIVERED = "livrata"

ROLE_ADMIN = "admin"
ROLE_EMPLOYEE = "employee"
ROLE_MANAGER = "manager"

NOTIF_DELAY = "delay"
NOTIF_DUE = "client_due"
NOTIF_REJECTED = "rejected_return"


db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Te rog autentifica-te."
scheduler = BackgroundScheduler(timezone=str(TZ))


class Section(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    flow_order = db.Column(db.Integer, nullable=False)
    deadline_days = db.Column(db.Integer, nullable=False, default=2)
    active = db.Column(db.Boolean, nullable=False, default=True)

    employees = db.relationship("User", backref="section", lazy=True)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=ROLE_EMPLOYEE)
    section_id = db.Column(db.Integer, db.ForeignKey("section.id"), nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == ROLE_ADMIN

    @property
    def is_manager(self):
        return self.role == ROLE_MANAGER


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(50), unique=True, nullable=False)
    client_name = db.Column(db.String(150), nullable=False)
    product_name = db.Column(db.String(150), nullable=False)
    dimensions = db.Column(db.String(120), nullable=True)
    finish = db.Column(db.String(120), nullable=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    due_date = db.Column(db.Date, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(50), nullable=False, default=ORDER_NEW)
    current_section_id = db.Column(db.Integer, db.ForeignKey("section.id"), nullable=True)
    current_employee_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    current_section = db.relationship("Section", foreign_keys=[current_section_id])
    current_employee = db.relationship("User", foreign_keys=[current_employee_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])


class Transfer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False)
    from_section_id = db.Column(db.Integer, db.ForeignKey("section.id"), nullable=True)
    to_section_id = db.Column(db.Integer, db.ForeignKey("section.id"), nullable=False)
    from_employee_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    to_employee_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    sent_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    responded_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(30), nullable=False, default=TRANSFER_SENT)
    sender_note = db.Column(db.Text, nullable=True)
    rejection_reason = db.Column(db.Text, nullable=True)
    is_return = db.Column(db.Boolean, nullable=False, default=False)

    order = db.relationship("Order", backref=db.backref("transfers", lazy=True, order_by="Transfer.sent_at.desc()"))
    from_section = db.relationship("Section", foreign_keys=[from_section_id])
    to_section = db.relationship("Section", foreign_keys=[to_section_id])
    from_employee = db.relationship("User", foreign_keys=[from_employee_id])
    to_employee = db.relationship("User", foreign_keys=[to_employee_id])


class HistoryEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False)
    event_type = db.Column(db.String(50), nullable=False)
    message = db.Column(db.Text, nullable=False)
    section_id = db.Column(db.Integer, db.ForeignKey("section.id"), nullable=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    order = db.relationship("Order", backref=db.backref("history_events", lazy=True, order_by="HistoryEvent.created_at.desc()"))
    section = db.relationship("Section")
    employee = db.relationship("User")


class Attachment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False)
    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    file_type = db.Column(db.String(20), nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    order = db.relationship("Order", backref=db.backref("attachments", lazy=True, order_by="Attachment.uploaded_at.desc()"))
    uploaded_by = db.relationship("User")


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False)
    notif_type = db.Column(db.String(50), nullable=False)
    notif_key = db.Column(db.String(255), nullable=False)
    message = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    resolved_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", backref=db.backref("notifications", lazy=True, order_by="Notification.created_at.desc()"))
    order = db.relationship("Order")


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def manager_or_admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in (ROLE_ADMIN, ROLE_MANAGER):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def status_label(status: str) -> str:
    mapping = {
        ORDER_NEW: "Noua",
        ORDER_IN_PROGRESS: "In lucru",
        ORDER_AWAITING_RECEIPT: "In asteptare receptie",
        ORDER_REJECTED: "Refuzata pentru remedieri",
        ORDER_COMPLETED: "Finalizata",
        ORDER_DELIVERED: "Livrata",
    }
    return mapping.get(status, status)


def role_label(role: str) -> str:
    return {
        ROLE_ADMIN: "admin",
        ROLE_EMPLOYEE: "employee",
        ROLE_MANAGER: "manager",
    }.get(role, role)


def transfer_status_label(status: str) -> str:
    return {
        TRANSFER_SENT: "Trimisa",
        TRANSFER_ACCEPTED: "Acceptata",
        TRANSFER_REJECTED: "Refuzata",
    }.get(status, status)


def add_history(order: Order, event_type: str, message: str, section_id=None, employee_id=None):
    db.session.add(HistoryEvent(order_id=order.id, event_type=event_type, message=message, section_id=section_id, employee_id=employee_id))


def working_days_between(start_dt: datetime, end_dt: datetime) -> int:
    if not start_dt or not end_dt:
        return 0
    start_d = start_dt.date()
    end_d = end_dt.date()
    if end_d <= start_d:
        return 0
    count = 0
    current = start_d + timedelta(days=1)
    while current <= end_d:
        if current.weekday() != 6:  # Sunday excluded
            count += 1
        current += timedelta(days=1)
    return count


def calculate_delay_days(start_dt: datetime, end_dt: datetime, allowed_days: int) -> int:
    return max(0, working_days_between(start_dt, end_dt) - max(0, allowed_days))


def get_last_accepted_transfer_for_order_in_section(order_id: int, section_id: int):
    return (
        Transfer.query.filter_by(order_id=order_id, to_section_id=section_id, status=TRANSFER_ACCEPTED)
        .order_by(Transfer.responded_at.desc())
        .first()
    )


def get_section_entry_datetime(order: Order, section_id: int):
    accepted_transfer = get_last_accepted_transfer_for_order_in_section(order.id, section_id)
    if accepted_transfer and accepted_transfer.responded_at:
        return accepted_transfer.responded_at
    return order.created_at


def current_section_delay(order: Order) -> int:
    if not order.current_section_id or order.status in (ORDER_COMPLETED, ORDER_DELIVERED, ORDER_AWAITING_RECEIPT):
        return 0
    start_dt = get_section_entry_datetime(order, order.current_section_id)
    return calculate_delay_days(start_dt, datetime.utcnow(), order.current_section.deadline_days if order.current_section else 0)


def create_delay_history_if_needed(order: Order, from_section: Section, accepted_at: datetime, sent_at: datetime, employee_id=None):
    if not from_section or not accepted_at:
        return
    late_days = calculate_delay_days(accepted_at, sent_at, from_section.deadline_days)
    if late_days > 0:
        add_history(
            order,
            "intarziere",
            f"Intarziere in sectia {from_section.name} ({late_days} zile)",
            section_id=from_section.id,
            employee_id=employee_id or order.current_employee_id,
        )


def next_flow_section(current_section: Section):
    if not current_section:
        return None
    return (
        Section.query.filter(Section.active.is_(True), Section.flow_order > current_section.flow_order)
        .order_by(Section.flow_order.asc())
        .first()
    )


def allowed_recipients_for(order: Order, sender: User):
    return User.query.filter_by(active=True, role=ROLE_EMPLOYEE).order_by(User.full_name.asc()).all()


def get_active_notifications_for(user_id: int):
    return Notification.query.filter_by(user_id=user_id, is_active=True).order_by(Notification.created_at.desc()).all()


def ensure_notification(user_id: int, order_id: int, notif_type: str, notif_key: str, message: str):
    existing = Notification.query.filter_by(user_id=user_id, notif_key=notif_key, is_active=True).first()
    if existing:
        existing.message = message
        return existing
    notif = Notification(user_id=user_id, order_id=order_id, notif_type=notif_type, notif_key=notif_key, message=message, is_active=True)
    db.session.add(notif)
    return notif


def resolve_notifications(prefix: str, order_id: int = None):
    q = Notification.query.filter(Notification.notif_key.like(f"{prefix}%"), Notification.is_active.is_(True))
    if order_id is not None:
        q = q.filter_by(order_id=order_id)
    for notif in q.all():
        notif.is_active = False
        notif.resolved_at = datetime.utcnow()


def sync_notifications(commit=True):
    now = datetime.utcnow()
    today = datetime.now(TZ).date()
    active_users = User.query.filter_by(active=True).all()
    for order in Order.query.all():
        # Section delay notifications
        delay = current_section_delay(order)
        delay_prefix = f"delay:{order.id}:"
        if delay > 0 and order.current_employee_id and order.current_section:
            message = f"Intarziere in sectia {order.current_section.name} ({delay} zile)"
            recipients = {order.current_employee_id}
            for u in active_users:
                if u.role in (ROLE_ADMIN, ROLE_MANAGER):
                    recipients.add(u.id)
            for user_id in recipients:
                ensure_notification(user_id, order.id, NOTIF_DELAY, f"delay:{order.id}:{user_id}", message)
        else:
            resolve_notifications(delay_prefix, order.id)

        # Client due notifications persist until delivered/finalized
        due_prefix = f"due:{order.id}:"
        if order.due_date and order.due_date < today and order.status not in (ORDER_DELIVERED,):
            message = f"Comanda {order.order_number} a depasit termenul de livrare"
            for u in active_users:
                ensure_notification(u.id, order.id, NOTIF_DUE, f"due:{order.id}:{u.id}", message)
        else:
            resolve_notifications(due_prefix, order.id)
    if commit:
        db.session.commit()


def run_daily_due_notifications():
    with app.app_context():
        sync_notifications(commit=True)


def seed_data():
    if Section.query.count() == 0:
        for name, order_no, days in SECTION_DEFAULTS:
            db.session.add(Section(name=name, flow_order=order_no, deadline_days=days, active=True))
        db.session.commit()
    else:
        existing_names = {s.name for s in Section.query.all()}
        for name, order_no, days in SECTION_DEFAULTS:
            if name not in existing_names:
                db.session.add(Section(name=name, flow_order=order_no, deadline_days=days, active=True))
        db.session.commit()

    if not User.query.filter_by(username="admin").first():
        admin = User(full_name="Administrator", username="admin", role=ROLE_ADMIN, active=True)
        admin.set_password("admin123")
        db.session.add(admin)
        db.session.commit()

    if not User.query.filter_by(username="manager").first():
        manager = User(full_name="Manager", username="manager", role=ROLE_MANAGER, active=True)
        manager.set_password("manager123")
        db.session.add(manager)
        db.session.commit()

    sections = {s.flow_order: s for s in Section.query.all()}
    default_names = [
        ("tocuri1", "Tocuri 1", 1), ("tocuri2", "Tocuri 2", 1),
        ("usicrude1", "Usi crude 1", 2), ("usicrude2", "Usi crude 2", 2), ("usicrude3", "Usi crude 3", 2), ("usicrude4", "Usi crude 4", 2),
        ("vopsea1", "Vopsea 1", 3), ("vopsea2", "Vopsea 2", 3), ("vopsea3", "Vopsea 3", 3), ("vopsea4", "Vopsea 4", 3),
        ("asamblare1", "Asamblare 1", 4), ("asamblare2", "Asamblare 2", 4), ("asamblare3", "Asamblare 3", 4), ("asamblare4", "Asamblare 4", 4),
        ("control1", "Control 1", 5), ("control2", "Control 2", 5),
        ("livrare1", "Livrare 1", 6), ("livrare2", "Livrare 2", 6),
    ]

    for username, full_name, flow_order in default_names:
        if not User.query.filter_by(username=username).first():
            user = User(
                full_name=full_name,
                username=username,
                role=ROLE_EMPLOYEE,
                section_id=sections[flow_order].id,
                active=True,
            )
            user.set_password("1234")
            db.session.add(user)
    db.session.commit()

    if Order.query.count() == 0:
        first_employee = User.query.filter_by(username="tocuri1").first()
        admin = User.query.filter_by(username="admin").first()

        order = Order(
            order_number="CMD-1001",
            client_name="Client Demo",
            product_name="Usa interior model A",
            dimensions="900x2100 mm",
            finish="Alb mat",
            quantity=5,
            due_date=date.today(),
            notes="Comanda demo pentru testare.",
            status=ORDER_AWAITING_RECEIPT,
            current_section_id=first_employee.section_id if first_employee else None,
            current_employee_id=first_employee.id if first_employee else None,
            created_by_id=admin.id if admin else None,
        )
        db.session.add(order)
        db.session.commit()

        if first_employee:
            transfer = Transfer(
                order_id=order.id,
                from_section_id=None,
                to_section_id=first_employee.section_id,
                from_employee_id=None,
                to_employee_id=first_employee.id,
                status=TRANSFER_SENT,
                sender_note="Comanda noua",
            )
            db.session.add(transfer)
            db.session.commit()

        add_history(
            order,
            "creare",
            f"Comanda creata si trimisa pentru acceptare lui {first_employee.full_name if first_employee else '-'}",
            section_id=order.current_section_id,
            employee_id=admin.id if admin else None,
        )
        db.session.commit()


def migrate_schema():
    insp = inspect(db.engine)
    cols = {c["name"] for c in insp.get_columns("user")}
    if "active" not in cols:
        db.session.execute(text("ALTER TABLE user ADD COLUMN active BOOLEAN DEFAULT 1"))
    cols = {c["name"] for c in insp.get_columns("user")}
    if "role" not in cols:
        db.session.execute(text("ALTER TABLE user ADD COLUMN role VARCHAR(20) DEFAULT 'employee'"))

    transfer_cols = {c["name"] for c in insp.get_columns("transfer")}
    if "is_return" not in transfer_cols:
        db.session.execute(text("ALTER TABLE transfer ADD COLUMN is_return BOOLEAN DEFAULT 0"))

    if "notification" not in insp.get_table_names():
        Notification.__table__.create(db.engine)
    db.session.commit()


@app.context_processor
def inject_globals():
    return {
        "status_label": status_label,
        "transfer_status_label": transfer_status_label,
        "role_label": role_label,
        "ROLE_ADMIN": ROLE_ADMIN,
        "ROLE_EMPLOYEE": ROLE_EMPLOYEE,
        "ROLE_MANAGER": ROLE_MANAGER,
    }


@app.route("/")
@login_required
def index():
    sync_notifications(commit=True)
    if current_user.is_admin or current_user.is_manager:
        total_orders = Order.query.count()
        in_progress = Order.query.filter(Order.status.in_([ORDER_IN_PROGRESS, ORDER_REJECTED])).count()
        awaiting = Order.query.filter_by(status=ORDER_AWAITING_RECEIPT).count()
        rejected = Transfer.query.filter_by(status=TRANSFER_REJECTED).count()
        recent_orders = Order.query.order_by(Order.updated_at.desc()).limit(8).all()
        section_stats = []
        for section in Section.query.filter_by(active=True).order_by(Section.flow_order.asc()).all():
            active_orders = Order.query.filter_by(current_section_id=section.id).filter(Order.status != ORDER_DELIVERED).count()
            delayed_orders = sum(1 for o in Order.query.filter_by(current_section_id=section.id).all() if current_section_delay(o) > 0)
            section_stats.append({
                "section": section,
                "active_orders": active_orders,
                "employees": User.query.filter_by(section_id=section.id, active=True, role=ROLE_EMPLOYEE).count(),
                "delayed_orders": delayed_orders,
            })
        notifications = get_active_notifications_for(current_user.id)
        return render_template(
            "dashboard_admin.html",
            total_orders=total_orders,
            in_progress=in_progress,
            awaiting=awaiting,
            rejected=rejected,
            recent_orders=recent_orders,
            section_stats=section_stats,
            notifications=notifications,
        )

    my_orders = Order.query.filter(
    Order.current_employee_id == current_user.id,
    Order.status != ORDER_DELIVERED
).order_by(Order.updated_at.desc()).all()
    inbox = Transfer.query.filter_by(to_employee_id=current_user.id, status=TRANSFER_SENT).order_by(Transfer.sent_at.desc()).all()
    notifications = get_active_notifications_for(current_user.id)
    past_orders = (
        Order.query.join(HistoryEvent, HistoryEvent.order_id == Order.id)
        .filter(HistoryEvent.employee_id == current_user.id)
        .order_by(HistoryEvent.created_at.desc())
        .distinct()
        .limit(20)
        .all()
    )
    return render_template("dashboard_employee.html", my_orders=my_orders, inbox=inbox, notifications=notifications, past_orders=past_orders)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username, active=True).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("index"))
        flash("Date de autentificare invalide.", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/orders")
@login_required
def orders():
    q = request.args.get("q", "").strip()
    query = Order.query
    if current_user.role == ROLE_EMPLOYEE:
        query = query.filter(
            or_(
                Order.current_employee_id == current_user.id,
                Order.created_by_id == current_user.id,
                Order.id.in_(db.session.query(HistoryEvent.order_id).filter_by(employee_id=current_user.id)),
            )
        )
    if q:
        query = query.filter(or_(Order.order_number.ilike(f"%{q}%"), Order.client_name.ilike(f"%{q}%"), Order.product_name.ilike(f"%{q}%")))
    orders_list = query.order_by(Order.updated_at.desc()).all()
    return render_template("orders.html", orders=orders_list, q=q)


@app.route("/orders/new", methods=["GET", "POST"])
@login_required
@manager_or_admin_required
def order_new():
    employees = User.query.filter_by(active=True, role=ROLE_EMPLOYEE).order_by(User.full_name.asc()).all()

    if request.method == "POST":
        employee = db.session.get(User, int(request.form.get("first_employee_id"))) if request.form.get("first_employee_id") else None

        if not employee:
            flash("Selecteaza un angajat valid.", "danger")
            return redirect(url_for("order_new"))

        order = Order(
            order_number=request.form.get("order_number", "").strip(),
            client_name=request.form.get("client_name", "").strip(),
            product_name=request.form.get("product_name", "").strip(),
            dimensions=request.form.get("dimensions", "").strip(),
            finish=request.form.get("finish", "").strip(),
            quantity=int(request.form.get("quantity") or 1),
            due_date=datetime.strptime(request.form.get("due_date"), "%Y-%m-%d").date() if request.form.get("due_date") else None,
            notes=request.form.get("notes", "").strip(),

            # 🔴 CHEIA AICI
            status=ORDER_AWAITING_RECEIPT,

            current_section_id=employee.section_id,
            current_employee_id=employee.id,
            created_by_id=current_user.id,
        )

        db.session.add(order)
        db.session.commit()

        # 🔴 CREARE TRANSFER PENTRU ACCEPTARE
        transfer = Transfer(
            order_id=order.id,
            from_section_id=None,
            to_section_id=employee.section_id,
            from_employee_id=None,
            to_employee_id=employee.id,
            status=TRANSFER_SENT,
            sender_note="Comanda noua",
        )
        db.session.add(transfer)
        db.session.commit()

        add_history(
            order,
            "creare",
            f"Comanda creata si trimisa pentru acceptare lui {employee.full_name}",
            section_id=employee.section_id,
            employee_id=current_user.id,
        )

        db.session.commit()

        flash("Comanda a fost creata si trimisa pentru acceptare.", "success")
        return redirect(url_for("order_detail", order_id=order.id))

    return render_template("order_form.html", employees=employees)


@app.route("/orders/<int:order_id>")
@login_required
def order_detail(order_id):
    order = db.session.get(Order, order_id)
    if not order:
        abort(404)
    if current_user.role == ROLE_EMPLOYEE:
        allowed = order.current_employee_id == current_user.id or Transfer.query.filter_by(order_id=order.id, to_employee_id=current_user.id).count() > 0 or HistoryEvent.query.filter_by(order_id=order.id, employee_id=current_user.id).count() > 0
        if not allowed:
            abort(403)
    employees = allowed_recipients_for(order, current_user) if current_user.role != ROLE_MANAGER else []
    next_section = next_flow_section(order.current_section)
    current_delay = current_section_delay(order)
    return render_template("order_detail.html", order=order, employees=employees, next_section=next_section, current_delay=current_delay)


@app.route("/orders/<int:order_id>/upload", methods=["POST"])
@login_required
def upload_attachment(order_id):
    order = db.session.get(Order, order_id)
    if not order:
        abort(404)
    file = request.files.get("file")
    if not file or file.filename == "":
        flash("Selecteaza un fisier.", "warning")
        return redirect(url_for("order_detail", order_id=order_id))
    if not allowed_file(file.filename):
        flash("Tip fisier neacceptat. Permise: PDF, JPG, PNG, WEBP.", "danger")
        return redirect(url_for("order_detail", order_id=order_id))

    ext = file.filename.rsplit(".", 1)[1].lower()
    original_name = secure_filename(file.filename)
    stored_name = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(app.config["UPLOAD_FOLDER"], stored_name)
    file.save(path)

    attachment = Attachment(order_id=order.id, original_name=original_name, stored_name=stored_name, file_type=ext, uploaded_by_id=current_user.id)
    db.session.add(attachment)
    add_history(order, "atasament", f"Fisier incarcat: {original_name}", section_id=order.current_section_id, employee_id=current_user.id)
    db.session.commit()
    flash("Fisier incarcat cu succes.", "success")
    return redirect(url_for("order_detail", order_id=order_id))


@app.route("/attachments/<int:attachment_id>")
@login_required
def download_attachment(attachment_id):
    attachment = db.session.get(Attachment, attachment_id)
    if not attachment:
        abort(404)
    return send_from_directory(app.config["UPLOAD_FOLDER"], attachment.stored_name, as_attachment=True, download_name=attachment.original_name)


@app.route("/orders/<int:order_id>/send", methods=["POST"])
@login_required
def send_order(order_id):
    order = db.session.get(Order, order_id)
    if not order:
        abort(404)
    if current_user.role == ROLE_MANAGER:
        abort(403)
    if not (current_user.is_admin or order.current_employee_id == current_user.id):
        abort(403)

    to_employee_id = int(request.form.get("to_employee_id"))
    sender_note = request.form.get("sender_note", "").strip()
    to_employee = db.session.get(User, to_employee_id)
    if not to_employee or not to_employee.active or to_employee.role != ROLE_EMPLOYEE:
        flash("Angajatul selectat nu este valid.", "danger")
        return redirect(url_for("order_detail", order_id=order.id))

    to_section = to_employee.section
    if not to_section:
        flash("Angajatul destinatar nu are sectie asignata.", "danger")
        return redirect(url_for("order_detail", order_id=order.id))


    accepted_at = get_section_entry_datetime(order, order.current_section_id) if order.current_section_id else order.created_at
    create_delay_history_if_needed(order, order.current_section, accepted_at, datetime.utcnow(), employee_id=current_user.id)

    transfer = Transfer(
        order_id=order.id,
        from_section_id=order.current_section_id,
        to_section_id=to_section.id,
        from_employee_id=order.current_employee_id,
        to_employee_id=to_employee_id,
        sender_note=sender_note,
        status=TRANSFER_SENT,
    )
    db.session.add(transfer)
    order.status = ORDER_AWAITING_RECEIPT
    add_history(order, "predare", f"Comanda trimisa catre {to_employee.full_name} / {to_section.name}", section_id=to_section.id, employee_id=current_user.id)
    resolve_notifications(f"delay:{order.id}:", order.id)
    db.session.commit()
    flash("Comanda a fost trimisa si asteapta receptia.", "success")
    return redirect(url_for("order_detail", order_id=order.id))


@app.route("/transfers/<int:transfer_id>/accept", methods=["POST"])
@login_required
def accept_transfer(transfer_id):
    transfer = db.session.get(Transfer, transfer_id)
    if not transfer:
        abort(404)
    if transfer.to_employee_id != current_user.id and not current_user.is_admin:
        abort(403)
    if transfer.status != TRANSFER_SENT:
        flash("Predarea a fost deja procesata.", "warning")
        return redirect(url_for("index"))

    order = transfer.order
    transfer.status = TRANSFER_ACCEPTED
    transfer.responded_at = datetime.utcnow()
    order.current_section_id = transfer.to_section_id
    order.current_employee_id = transfer.to_employee_id
    order.status = ORDER_IN_PROGRESS
    if transfer.is_return:
        add_history(order, "acceptare_retur", f"Comanda returnata a fost acceptata de {transfer.to_employee.full_name}", section_id=transfer.to_section_id, employee_id=transfer.to_employee_id)
        resolve_notifications(f"rejected:{order.id}:", order.id)
    else:
        add_history(order, "acceptare", f"Comanda acceptata de {transfer.to_employee.full_name} in {transfer.to_section.name}", section_id=transfer.to_section_id, employee_id=transfer.to_employee_id)
    db.session.commit()
    flash("Comanda a fost receptionata.", "success")
    return redirect(url_for("index"))


@app.route("/transfers/<int:transfer_id>/reject", methods=["POST"])
@login_required
def reject_transfer(transfer_id):
    transfer = db.session.get(Transfer, transfer_id)
    if not transfer:
        abort(404)
    if transfer.to_employee_id != current_user.id and not current_user.is_admin:
        abort(403)
    if transfer.status != TRANSFER_SENT:
        flash("Predarea a fost deja procesata.", "warning")
        return redirect(url_for("index"))

    reason = request.form.get("rejection_reason", "").strip()
    if not reason:
        flash("Motivul refuzului este obligatoriu.", "danger")
        return redirect(url_for("index"))

    order = transfer.order
    transfer.status = TRANSFER_REJECTED
    transfer.responded_at = datetime.utcnow()
    transfer.rejection_reason = reason

    return_transfer = Transfer(
        order_id=order.id,
        from_section_id=transfer.to_section_id,
        to_section_id=transfer.from_section_id or transfer.to_section_id,
        from_employee_id=transfer.to_employee_id,
        to_employee_id=transfer.from_employee_id or transfer.to_employee_id,
        sender_note=f"Refuz pentru remedieri: {reason}",
        status=TRANSFER_SENT,
        is_return=True,
    )
    db.session.add(return_transfer)
    order.current_section_id = transfer.from_section_id or order.current_section_id
    order.current_employee_id = transfer.from_employee_id or order.current_employee_id
    order.status = ORDER_AWAITING_RECEIPT
    add_history(order, "refuz", f"Refuzata de {transfer.to_employee.full_name}: {reason}", section_id=transfer.from_section_id, employee_id=transfer.to_employee_id)
    ensure_notification(return_transfer.to_employee_id, order.id, NOTIF_REJECTED, f"rejected:{order.id}:{return_transfer.to_employee_id}", f"Comanda {order.order_number} a fost refuzata si necesita acceptare pentru remedieri")
    db.session.commit()
    flash("Comanda a fost refuzata si returnata pentru remedieri.", "warning")
    return redirect(url_for("index"))


@app.route("/orders/<int:order_id>/complete", methods=["POST"])
@login_required
def complete_order(order_id):
    order = db.session.get(Order, order_id)
    if not order:
        abort(404)
    if current_user.role == ROLE_MANAGER:
        abort(403)
    if not (current_user.is_admin or order.current_employee_id == current_user.id):
        abort(403)
    
    accepted_at = get_section_entry_datetime(order, order.current_section_id) if order.current_section_id else order.created_at
    create_delay_history_if_needed(order, order.current_section, accepted_at, datetime.utcnow(), employee_id=current_user.id)

    order.status = ORDER_DELIVERED
    add_history(order, "livrare", "Comanda a fost marcata ca livrata", section_id=order.current_section_id, employee_id=current_user.id)
    resolve_notifications(f"due:{order.id}:", order.id)
    resolve_notifications(f"delay:{order.id}:", order.id)
    db.session.commit()
    flash("Comanda a fost marcata ca livrata.", "success")
    return redirect(url_for("order_detail", order_id=order.id))


@app.route("/admin/employees")
@login_required
@admin_required
def employees():
    employees_list = User.query.order_by(User.active.desc(), User.role.desc(), User.full_name.asc()).all()
    sections = Section.query.order_by(Section.flow_order.asc()).all()
    return render_template("employees.html", employees=employees_list, sections=sections)


@app.route("/admin/employees/new", methods=["POST"])
@login_required
@admin_required
def employee_new():
    user = User(
        full_name=request.form.get("full_name", "").strip(),
        username=request.form.get("username", "").strip(),
        role=request.form.get("role", ROLE_EMPLOYEE),
        section_id=int(request.form.get("section_id")) if request.form.get("section_id") else None,
        active=True,
    )
    password = request.form.get("password", "1234")
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    flash("Angajat adaugat.", "success")
    return redirect(url_for("employees"))


@app.route("/admin/employees/<int:user_id>/update", methods=["POST"])
@login_required
@admin_required
def employee_update(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    user.full_name = request.form.get("full_name", user.full_name).strip()
    user.username = request.form.get("username", user.username).strip()
    user.role = request.form.get("role", user.role)
    user.section_id = int(request.form.get("section_id")) if request.form.get("section_id") else None
    new_password = request.form.get("password", "").strip()
    if new_password:
        user.set_password(new_password)
    db.session.commit()
    flash("Angajat modificat.", "success")
    return redirect(url_for("employees"))


@app.route("/admin/employees/<int:user_id>/archive", methods=["POST"])
@login_required
@admin_required
def employee_archive(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    if user.username == "admin":
        flash("Administratorul principal nu poate fi arhivat.", "danger")
        return redirect(url_for("employees"))
    user.active = False
    db.session.commit()
    flash("Angajat arhivat.", "success")
    return redirect(url_for("employees"))


@app.route("/admin/settings", methods=["GET", "POST"])
@login_required
@admin_required
def admin_settings():
    sections = Section.query.order_by(Section.flow_order.asc()).all()
    if request.method == "POST":
        for section in sections:
            val = request.form.get(f"deadline_{section.id}")
            try:
                section.deadline_days = max(0, int(val))
            except (TypeError, ValueError):
                pass
        db.session.commit()
        flash("Setarile au fost salvate.", "success")
        return redirect(url_for("admin_settings"))
    return render_template("settings.html", sections=sections)


@app.route("/admin/history")
@login_required
@manager_or_admin_required
def admin_history():
    events = HistoryEvent.query.order_by(HistoryEvent.created_at.desc()).limit(300).all()
    return render_template("history.html", events=events)


@app.route("/reports")
@login_required
@manager_or_admin_required
def reports():
    start_raw = request.args.get("start")
    end_raw = request.args.get("end")
    start_date = datetime.strptime(start_raw, "%Y-%m-%d").date() if start_raw else date.today() - timedelta(days=30)
    end_date = datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else date.today()
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time())

    section_reports = []
    for section in Section.query.filter_by(active=True).order_by(Section.flow_order.asc()).all():
        in_work = Order.query.filter_by(current_section_id=section.id).filter(Order.status.in_([ORDER_IN_PROGRESS, ORDER_REJECTED, ORDER_AWAITING_RECEIPT])).count()
        delayed = sum(1 for o in Order.query.filter_by(current_section_id=section.id).all() if current_section_delay(o) > 0)
        executed = HistoryEvent.query.filter_by(section_id=section.id, event_type="acceptare").filter(HistoryEvent.created_at.between(start_dt, end_dt)).count()
        sent_forward = HistoryEvent.query.filter_by(section_id=section.id, event_type="predare").filter(HistoryEvent.created_at.between(start_dt, end_dt)).count()
        section_reports.append({"section": section, "in_work": in_work, "delayed": delayed, "executed": executed, "sent_forward": sent_forward})

    employee_reports = []
    for employee in User.query.filter_by(active=True, role=ROLE_EMPLOYEE).order_by(User.full_name.asc()).all():
        in_work = Order.query.filter_by(current_employee_id=employee.id).filter(Order.status.in_([ORDER_IN_PROGRESS, ORDER_REJECTED, ORDER_AWAITING_RECEIPT])).count()
        delayed = sum(1 for o in Order.query.filter_by(current_employee_id=employee.id).all() if current_section_delay(o) > 0)
        executed = HistoryEvent.query.filter_by(employee_id=employee.id).filter(HistoryEvent.event_type.in_(["acceptare", "acceptare_retur", "livrare"])).filter(HistoryEvent.created_at.between(start_dt, end_dt)).count()
        sent_forward = HistoryEvent.query.filter_by(employee_id=employee.id, event_type="predare").filter(HistoryEvent.created_at.between(start_dt, end_dt)).count()
        employee_reports.append({"employee": employee, "in_work": in_work, "delayed": delayed, "executed": executed, "sent_forward": sent_forward})

    return render_template("reports.html", section_reports=section_reports, employee_reports=employee_reports, start=start_date.isoformat(), end=end_date.isoformat())


@app.route("/my-history")
@login_required
def my_history():
    if current_user.role != ROLE_EMPLOYEE:
        return redirect(url_for("admin_history"))
    events = HistoryEvent.query.filter_by(employee_id=current_user.id).order_by(HistoryEvent.created_at.desc()).all()
    return render_template("history.html", events=events, employee_view=True)


@app.route("/health")
def health():
    return {"status": "ok"}


with app.app_context():
    db.create_all()
    migrate_schema()
    seed_data()
    sync_notifications(commit=True)

if not scheduler.running:
    scheduler.add_job(run_daily_due_notifications, CronTrigger(hour=9, minute=0))
    scheduler.add_job(run_daily_due_notifications, "interval", hours=1, id="hourly_sync", replace_existing=True)
    scheduler.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
