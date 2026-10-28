"""LP Platform — plataforma multi-tenant de landing pages, TODO o Python neste arquivo.

  Dev:   python app.py            (servidor local; use DEBUG=1)
  Prod:  gunicorn app:app
  CLI:   python app.py init-db | criar-tenant | listar-tenants | listar-sites | resetar-senha | gerar-hash

Procure por "# ═══" para pular entre as seções. Regra: toda consulta de leads/posts filtra por tenant_id.
"""
import argparse
import csv
import errno
import getpass
import hmac
import importlib.util
import io
import json
import logging
import markdown as _markdown
import nh3
import os
import random
import re
import requests
import secrets
import shutil
import smtplib
import sqlite3
import sys
import threading
import time
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from PIL import Image, ImageOps
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timezone
from dotenv import load_dotenv
from email.message import EmailMessage
from email.utils import format_datetime
from flask import Blueprint, Flask, Response, abort, current_app, flash, g, jsonify, redirect, render_template, request, send_file, send_from_directory, session, url_for
from flask_sqlalchemy import SQLAlchemy
from itsdangerous import BadData, URLSafeTimedSerializer
from jinja2 import ChoiceLoader, FileSystemLoader, PrefixLoader
from markupsafe import escape
from pathlib import Path
from sqlalchemy import delete, event, func, inspect, or_, select, text
from sqlalchemy.engine import Engine
from urllib.parse import quote, urlparse
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from xml.sax.saxutils import escape as xml_escape
from zoneinfo import ZoneInfo


# ═══════════════════════════════════════════════════════════════════════════
# CONFIG — Configuração central. Tudo vem de variáveis de ambiente (.env).
# ═══════════════════════════════════════════════════════════════════════════
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _bool(nome, padrao=False):
    v = os.environ.get(nome)
    if v is None or v.strip() == "":
        return padrao
    return v.strip().lower() in ("1", "true", "yes", "sim", "on")


def _int(nome, padrao):
    try:
        return int(os.environ.get(nome, "").strip() or padrao)
    except ValueError:
        return padrao


def _secret_key(debug):
    chave = os.environ.get("SECRET_KEY", "").strip()
    if not chave:
        if debug:
            return "dev-inseguro-nao-use-em-producao"
        raise RuntimeError("SECRET_KEY não definida. Gere com: python -c \"import secrets; print(secrets.token_hex(32))\" "
                           "e coloque no .env (obrigatória fora do modo DEBUG).")
    if not debug and (len(chave) < 32 or chave.lower().startswith("troque")):
        raise RuntimeError("SECRET_KEY fraca ou de exemplo. Use pelo menos 32 caracteres aleatórios.")
    return chave


def _db_url(data_dir):
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return "sqlite:///" + os.path.join(data_dir, "db.sqlite")
    if url.startswith("postgres://"):
        url = "postgresql+psycopg2://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


_DEBUG = _bool("DEBUG")
_DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR", "/data"))
_DB_URL = _db_url(_DATA_DIR)


class Config:
    DEBUG = _DEBUG
    SECRET_KEY = None                        # definida em create_app (falha cedo se faltar)
    DATA_DIR = _DATA_DIR
    UPLOAD_DIR = os.path.join(_DATA_DIR, "uploads")
    SITES_DIR = str(BASE_DIR / "sites")
    SQLALCHEMY_DATABASE_URI = _DB_URL
    SQLALCHEMY_ENGINE_OPTIONS = ({"connect_args": {"timeout": 15}} if _DB_URL.startswith("sqlite")
                                 else {"pool_pre_ping": True})
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_CONTENT_LENGTH = _int("MAX_CONTENT_MB", 32) * 1024 * 1024
    IMG_CONCORRENCIA = max(1, _int("IMG_CONCORRENCIA", 2))
    PROXY_HOPS = _int("PROXY_HOPS", 1)
    ADMIN_HOST = os.environ.get("ADMIN_HOST", "").strip().lower().split(":")[0]
    ALLOW_TENANT_OVERRIDE = _DEBUG or _bool("ALLOW_TENANT_OVERRIDE")
    TENANT_CACHE_TTL = _int("TENANT_CACHE_TTL", 30)
    DOMINIO_OK_PUBLICO = _bool("DOMINIO_OK_PUBLICO")
    ADMIN_USER = os.environ.get("ADMIN_USER", "").strip()
    ADMIN_PASS_HASH = os.environ.get("ADMIN_PASS_HASH", "").strip()
    SESSION_COOKIE_NAME = "lp_sessao"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("COOKIE_SECURE", not _DEBUG)
    PERMANENT_SESSION_LIFETIME = _int("SESSAO_HORAS", 12) * 3600
    PREFERRED_URL_SCHEME = "http" if _DEBUG else "https"
    BLOCK_BOTS = _bool("BLOCK_BOTS", True)
    LEAD_RATE_MAX = _int("LEAD_RATE_MAX", 5)
    LEAD_RATE_JANELA = _int("LEAD_RATE_JANELA", 600)
    LOGIN_RATE_MAX = _int("LOGIN_RATE_MAX", 6)
    LOGIN_RATE_JANELA = _int("LOGIN_RATE_JANELA", 600)
    SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
    SMTP_PORT = _int("SMTP_PORT", 587)
    SMTP_USER = os.environ.get("SMTP_USER", "").strip()
    SMTP_PASS = os.environ.get("SMTP_PASS", "")
    SMTP_SECURITY = os.environ.get("SMTP_SECURITY", "starttls").strip().lower()
    SMTP_FROM = os.environ.get("SMTP_FROM", "").strip()
    TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Sao_Paulo"))


# ═══════════════════════════════════════════════════════════════════════════
# EXTENSIONS
# ═══════════════════════════════════════════════════════════════════════════
db = SQLAlchemy()


@event.listens_for(Engine, "connect")
def _pragmas_sqlite(conexao, _registro):
    """SQLite: WAL, busy_timeout (espera em vez de 'database is locked') e FKs ligadas."""
    if isinstance(conexao, sqlite3.Connection):
        cur = conexao.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


# ═══════════════════════════════════════════════════════════════════════════
# MODELS — Modelos. REGRA: toda consulta de leads/posts filtra por tenant_id.
# ═══════════════════════════════════════════════════════════════════════════
def agora_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Tenant(db.Model):
    __tablename__ = "tenants"
    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(40), unique=True, nullable=False, index=True)
    nome = db.Column(db.String(120), nullable=False)
    template = db.Column(db.String(60), nullable=False)
    senha_hash = db.Column(db.String(255), nullable=False)
    sessao_versao = db.Column(db.Integer, nullable=False, default=1)
    email_aviso = db.Column(db.String(254))
    whatsapp = db.Column(db.String(20))
    webhook_url = db.Column(db.String(500))
    ativo = db.Column(db.Boolean, nullable=False, default=True)
    criado_em = db.Column(db.DateTime, nullable=False, default=agora_utc)
    dominios = db.relationship("TenantDominio", backref="tenant", cascade="all, delete-orphan",
                               order_by="TenantDominio.id", lazy="selectin")

    @property
    def dominios_lista(self):
        return [d.dominio for d in self.dominios]


class TenantDominio(db.Model):
    __tablename__ = "tenant_dominios"
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    dominio = db.Column(db.String(253), unique=True, nullable=False, index=True)


class Lead(db.Model):
    __tablename__ = "leads"
    __table_args__ = (db.Index("ix_leads_tenant_data", "tenant_id", "criado_em"),)
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    nome = db.Column(db.String(120))
    telefone = db.Column(db.String(30))
    email = db.Column(db.String(254))
    mensagem = db.Column(db.Text)
    extras = db.Column(db.JSON)
    utm_source = db.Column(db.String(200))
    utm_medium = db.Column(db.String(200))
    utm_campaign = db.Column(db.String(200))
    utm_term = db.Column(db.String(200))
    utm_content = db.Column(db.String(200))
    referrer = db.Column(db.String(500))
    pagina = db.Column(db.String(500))
    user_agent = db.Column(db.String(300))
    ip = db.Column(db.String(64))
    atendido = db.Column(db.Boolean, nullable=False, default=False)
    notif_email = db.Column(db.String(200))
    notif_webhook = db.Column(db.String(200))
    criado_em = db.Column(db.DateTime, nullable=False, default=agora_utc)


class Post(db.Model):
    __tablename__ = "posts"
    __table_args__ = (db.UniqueConstraint("tenant_id", "slug", name="uq_post_tenant_slug"),
                      db.Index("ix_posts_tenant_status", "tenant_id", "status", "publicado_em"))
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    titulo = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(120), nullable=False)
    resumo = db.Column(db.String(500))
    formato = db.Column(db.String(10), nullable=False, default="html")
    corpo = db.Column(db.Text, nullable=False, default="")
    corpo_html = db.Column(db.Text, nullable=False, default="")
    capa = db.Column(db.String(100))
    meta_description = db.Column(db.String(300))
    status = db.Column(db.String(12), nullable=False, default="rascunho")
    publicado_em = db.Column(db.DateTime)
    criado_em = db.Column(db.DateTime, nullable=False, default=agora_utc)
    atualizado_em = db.Column(db.DateTime, nullable=False, default=agora_utc, onupdate=agora_utc)


class RateHit(db.Model):
    """Rate limit persistente (compartilhado entre workers)."""
    __tablename__ = "rate_hits"
    __table_args__ = (db.Index("ix_rate_chave_ts", "chave", "ts"),)
    id = db.Column(db.Integer, primary_key=True)
    chave = db.Column(db.String(120), nullable=False)
    ts = db.Column(db.Float, nullable=False)


class SchemaInfo(db.Model):
    __tablename__ = "schema_info"
    id = db.Column(db.Integer, primary_key=True)
    versao = db.Column(db.Integer, nullable=False, default=0)


# ═══════════════════════════════════════════════════════════════════════════
# UTILS — Utilitários: sanitização, slug, CSRF, datas, HTML seguro.
# ═══════════════════════════════════════════════════════════════════════════
CODIGO_RE = re.compile(r"^lp[a-z0-9_]+$")
DOMINIO_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


def sanitize_input(txt):
    txt = re.sub(r"<[^>]*>", "", str(txt or ""))
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", txt).strip()


def gerar_slug(txt, limite=80):
    txt = unicodedata.normalize("NFKD", str(txt or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", txt.lower()).strip("-")[:limite].strip("-") or "item"


def so_digitos(txt):
    return re.sub(r"\D", "", str(txt or ""))


def eh_email(v):
    return bool(v) and len(v) <= 254 and bool(EMAIL_RE.match(v))


def telefone_valido(v):
    return 10 <= len(so_digitos(v)) <= 13


def normalizar_whatsapp(v):
    """Só dígitos com DDI 55. Vazio → ''. Inválido → None."""
    d = so_digitos(v)
    if not d:
        return ""
    if len(d) in (10, 11):
        d = "55" + d
    return d if 12 <= len(d) <= 13 else None


def wa_link(numero, texto=""):
    d = normalizar_whatsapp(numero)
    if not d:
        return ""
    return f"https://wa.me/{d}" + (f"?text={quote(texto)}" if texto else "")


def fmt_bytes(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        n = 0.0
    if n <= 0:
        return "0 B"
    if n < 1024:
        return f"{int(n)} B"
    for un in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or un == "TB":
            break
    txt = f"{n:.0f}" if (un in ("KB", "MB") and n >= 10) else f"{n:.1f}"
    if txt.endswith(".0") and un in ("KB", "MB"):
        txt = txt[:-2]
    return f"{txt.replace('.', ',')} {un}"


def escape_like(txt):
    return txt.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def safe_next(url):
    """Só caminho relativo do próprio site (evita open redirect)."""
    if not url or not url.startswith("/") or url.startswith("//") or "\\" in url:
        return None
    p = urlparse(url)
    return url if not p.scheme and not p.netloc else None


def normalizar_host(valor):
    """'WWW.Julia.com.br:8000' → 'julia.com.br'."""
    h = (valor or "").strip().lower().split("/")[0].split(":")[0].rstrip(".")
    return h[4:] if h.startswith("www.") else h


def para_local(dt):
    if not dt:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(current_app.config["TZ"])


def local_para_utc(dt_naive):
    return dt_naive.replace(tzinfo=current_app.config["TZ"]).astimezone(timezone.utc).replace(tzinfo=None)


def dia_local_para_utc(d: date, fim=False):
    return local_para_utc(datetime.combine(d, dtime.max if fim else dtime.min))


def parse_data(txt):
    try:
        return datetime.strptime((txt or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_datetime_local(txt):
    for f in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime((txt or "").strip(), f)
        except ValueError:
            pass
    return None


def filtro_data_br(v, hora=True):
    if not v:
        return "—"
    return para_local(v).strftime("%d/%m/%Y" + (" %H:%M" if hora else ""))


def filtro_tempo(v):
    if not v:
        return ""
    v = para_local(v)
    s = (datetime.now(current_app.config["TZ"]) - v).total_seconds()
    if s < 60:
        return "agora"
    if s < 3600:
        return f"há {int(s // 60)} min"
    if s < 86400:
        return f"há {int(s // 3600)} h"
    if s < 172800:
        return "ontem"
    return v.strftime("%d/%m/%Y")


# CSRF (token por sessão)
def csrf_token():
    t = session.get("_csrf")
    if not t:
        t = secrets.token_urlsafe(32)
        session["_csrf"] = t
    return t


def csrf_validar():
    esperado = session.get("_csrf")
    enviado = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
    return bool(esperado and enviado and hmac.compare_digest(esperado, enviado))


def exigir_csrf():
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and not csrf_validar():
        abort(400)


# HTML seguro (posts do blog)
TAGS_OK = {"p", "br", "hr", "h2", "h3", "h4", "h5", "ul", "ol", "li", "strong", "em", "b", "i", "u", "s", "a",
           "blockquote", "img", "figure", "figcaption", "code", "pre", "table", "thead", "tbody", "tr", "th", "td",
           "span", "div", "sup", "sub"}
ATRIBUTOS_OK = {"a": {"href", "title", "target"}, "img": {"src", "alt", "title", "width", "height", "loading"},
                "th": {"colspan", "rowspan"}, "td": {"colspan", "rowspan"}}


def html_seguro(html, codigo_tenant):
    """Sanitiza o corpo. Imagens só do próprio tenant (/m/<codigo>/...) ou https://."""
    prefixo = f"/m/{codigo_tenant}/"

    def filtro(tag, attr, valor):
        if tag == "img" and attr == "src":
            return valor if (valor.startswith(prefixo) or valor.startswith("https://")) else None
        if tag == "a" and attr == "target":
            return "_blank" if valor == "_blank" else None
        return valor

    return nh3.clean(html or "", tags=TAGS_OK, attributes=ATRIBUTOS_OK, attribute_filter=filtro,
                     url_schemes={"http", "https", "mailto", "tel"}, link_rel="noopener noreferrer nofollow")


def markdown_html(txt, codigo_tenant):
    return html_seguro(_markdown.markdown(txt or "", extensions=["extra", "sane_lists", "nl2br"]), codigo_tenant)


def texto_puro(html, limite=160):
    t = re.sub(r"\s+", " ", nh3.clean(html or "", tags=set())).strip()
    return t if len(t) <= limite else t[: limite - 1].rsplit(" ", 1)[0] + "…"


# ═══════════════════════════════════════════════════════════════════════════
# DBINIT — Criação/migração idempotente do banco (o entrypoint do Docker roda sempre que o container sobe).
# ═══════════════════════════════════════════════════════════════════════════
def adicionar_coluna(tabela, coluna, ddl):
    """Helper para migrações futuras: ALTER TABLE ADD COLUMN só se a coluna não existe."""
    if coluna not in {c["name"] for c in inspect(db.engine).get_columns(tabela)}:
        db.session.execute(text(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {ddl}"))
        db.session.commit()


# Exemplo: def _v2(): adicionar_coluna("tenants", "plano", "VARCHAR(20)")   →   MIGRACOES = [(2, _v2)]
MIGRACOES = []


def iniciar_banco(app):
    with app.app_context():
        os.makedirs(app.config["DATA_DIR"], exist_ok=True)
        os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
        db.create_all()
        info = db.session.get(SchemaInfo, 1)
        if info is None:
            info = SchemaInfo(id=1, versao=1)
            db.session.add(info)
            db.session.commit()
        for versao, fn in MIGRACOES:
            if versao > info.versao:
                fn()
                info.versao = versao
                db.session.commit()
        return info.versao


# ═══════════════════════════════════════════════════════════════════════════
# SITES — Descoberta dos sites (código do desenvolvedor) em sites/*/site.py: SLOTS de imagem e META.
# ═══════════════════════════════════════════════════════════════════════════
log_sites = logging.getLogger(__name__)
TEMPLATE_RE = re.compile(r"^lp_[a-z0-9_]+$")
SLOT_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
FORMATOS = ("auto", "png", "jpg", "webp")
_registro = {}
_lock_sites = threading.Lock()
_sites_dir = None


class SiteDef:
    def __init__(self, template, pasta, slots, meta, mtime):
        self.template, self.pasta, self.slots, self.meta, self.mtime = template, pasta, slots, meta, mtime

    @property
    def titulo(self):
        return self.meta.get("nome") or self.template


def _normalizar_slots(bruto):
    slots = {}
    if not isinstance(bruto, dict):
        return slots
    for chave, cfg in bruto.items():
        if not isinstance(chave, str) or not SLOT_RE.match(chave):
            log_sites.warning("Slot ignorado (use a-z, 0-9 e _): %r", chave)
            continue
        cfg = cfg if isinstance(cfg, dict) else {}
        try:
            maximo = max(100, min(int(cfg.get("max", 1600)), 4000))
        except (TypeError, ValueError):
            maximo = 1600
        formato = cfg.get("formato", "auto")
        slots[chave] = {"nome": str(cfg.get("nome") or chave.replace("_", " ").capitalize()), "max": maximo,
                        "svg": bool(cfg.get("svg", False)), "dica": str(cfg.get("dica", "")),
                        "formato": formato if formato in FORMATOS else "auto"}
    return slots


def _carregar(template):
    arq = os.path.join(_sites_dir, template, "site.py")
    try:
        mtime = os.stat(arq).st_mtime_ns
    except OSError:
        return None
    spec = importlib.util.spec_from_file_location(f"site_manifesto_{template}", arq)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        log_sites.exception("Erro ao carregar %s", arq)
        return None
    meta = getattr(mod, "META", {}) or {}
    return SiteDef(template, os.path.dirname(arq), _normalizar_slots(getattr(mod, "SLOTS", {})),
                   meta if isinstance(meta, dict) else {}, mtime)


def descobrir(sites_dir):
    global _sites_dir
    _sites_dir = sites_dir
    novo = {}
    if os.path.isdir(sites_dir):
        for nome in sorted(os.listdir(sites_dir)):
            if TEMPLATE_RE.match(nome) and os.path.isfile(os.path.join(sites_dir, nome, "site.py")):
                s = _carregar(nome)
                if s:
                    novo[nome] = s
    with _lock_sites:
        _registro.clear()
        _registro.update(novo)
    log_sites.info("Sites encontrados: %s", ", ".join(novo) or "(nenhum)")
    return novo


def obter(template):
    """SiteDef do template (recarrega se o site.py mudou em disco)."""
    if not template or not TEMPLATE_RE.match(template) or _sites_dir is None:
        return None
    s = _registro.get(template)
    try:
        mtime = os.stat(os.path.join(_sites_dir, template, "site.py")).st_mtime_ns
    except OSError:
        _registro.pop(template, None)
        return None
    if s is None or s.mtime != mtime:
        s = _carregar(template)
        with _lock_sites:
            if s:
                _registro[template] = s
            else:
                _registro.pop(template, None)
    return s


def listar():
    return [(t, s.titulo) for t, s in sorted(_registro.items())]


def arquivo_existe(template, nome):
    if not TEMPLATE_RE.match(template or "") or not re.match(r"^[A-Za-z0-9_.-]+$", nome):
        return False
    return os.path.isfile(os.path.join(_sites_dir or "", template, nome))


# ═══════════════════════════════════════════════════════════════════════════
# STORAGE — Imagens em volume: /data/uploads/<codigo_tenant>/ (nada externo). Slots, SVG sanitizado, variantes com WEBP.
# ═══════════════════════════════════════════════════════════════════════════
LIMITE_BYTES = 15 * 1024 * 1024
LIMITE_SVG = 2 * 1024 * 1024
IMG_MAX_PIXELS = 100_000_000
Image.MAX_IMAGE_PIXELS = IMG_MAX_PIXELS
LARGURAS = (400, 800, 1200, 1600)
FORMATOS_OK = {"JPEG", "PNG", "WEBP"}
MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "svg": "image/svg+xml"}
NOME_RE = re.compile(r"^[a-z0-9_-]{1,90}\.(png|jpg|webp|svg)$")
PREFIXO_GALERIA = "img-"
_sem = None
_sem_lock = threading.Lock()
_indices = {}


class UploadError(ValueError):
    """Mensagem pronta para o cliente (português)."""


def _semaforo():
    global _sem
    with _sem_lock:
        if _sem is None:
            _sem = threading.BoundedSemaphore(current_app.config["IMG_CONCORRENCIA"])
        return _sem


def pasta_tenant(codigo):
    if not CODIGO_RE.match(codigo or ""):
        raise ValueError("código de tenant inválido")
    raiz = os.path.realpath(current_app.config["UPLOAD_DIR"])
    pasta = os.path.realpath(os.path.join(raiz, codigo))
    if os.path.dirname(pasta) != raiz:
        raise ValueError("caminho inválido")
    return pasta


def caminho_seguro(codigo, nome):
    if not NOME_RE.match(nome or ""):
        return None
    return os.path.join(pasta_tenant(codigo), nome)


def indice(codigo):
    """{'hero': ('hero.webp', mtime)} — recalcula só quando a pasta muda."""
    pasta = pasta_tenant(codigo)
    try:
        t = os.stat(pasta).st_mtime_ns
    except OSError:
        return {}
    cache = _indices.get(codigo)
    if cache and cache[0] == t:
        return cache[1]
    idx = {}
    try:
        for f in os.listdir(pasta):
            if NOME_RE.match(f):
                try:
                    idx[f.rsplit(".", 1)[0]] = (f, int(os.path.getmtime(os.path.join(pasta, f))))
                except OSError:
                    pass
    except OSError:
        return {}
    _indices[codigo] = (t, idx)
    return idx


def invalidar_indice(codigo=None):
    if codigo:
        _indices.pop(codigo, None)
    else:
        _indices.clear()


def listar_galeria(codigo):
    itens, pasta = [], pasta_tenant(codigo)
    for stem, (arq, mtime) in indice(codigo).items():
        if stem.startswith(PREFIXO_GALERIA):
            try:
                tam = os.path.getsize(os.path.join(pasta, arq))
            except OSError:
                tam = 0
            itens.append({"nome": arq, "mtime": mtime, "tamanho": tam})
    return sorted(itens, key=lambda i: i["mtime"], reverse=True)


def _ler_dados(arquivo):
    if arquivo is None or not getattr(arquivo, "filename", ""):
        raise UploadError("Nenhum arquivo selecionado.")
    dados = arquivo.stream.read(LIMITE_BYTES + 1)
    if not dados:
        raise UploadError("O arquivo está vazio.")
    if len(dados) > LIMITE_BYTES:
        raise UploadError("O arquivo é maior que 15 MB. Reduza a imagem e tente novamente.")
    return dados


_SVG_PROIBIDO = re.compile(
    r"<script|javascript:|vbscript:|data:text/html|\son\w+\s*=|<foreignobject|<iframe|<embed|<object|<image"
    r"|<!doctype|<!entity|@import|url\(\s*[\"']?\s*(https?:)?//|href\s*=\s*[\"']\s*(https?:)?//", re.I)


def _processar_svg(dados, permitido):
    if not permitido:
        raise UploadError("Este campo não aceita SVG. Envie uma imagem PNG, JPG ou WEBP.")
    if len(dados) > LIMITE_SVG:
        raise UploadError("O SVG é maior que 2 MB.")
    try:
        txt = dados.decode("utf-8")
    except UnicodeDecodeError:
        raise UploadError("SVG inválido (salve o arquivo com codificação UTF-8).")
    if _SVG_PROIBIDO.search(txt):
        raise UploadError("O SVG contém elementos não permitidos (scripts, imagens embutidas ou links externos).")
    try:
        raiz = ET.fromstring(txt)
    except ET.ParseError:
        raise UploadError("O SVG está malformado.")
    if raiz.tag.rsplit("}", 1)[-1] != "svg":
        raise UploadError("O arquivo não é um SVG válido.")
    return txt.encode("utf-8"), "svg"


def _tem_transparencia(im):
    if im.mode in ("RGBA", "LA"):
        return im.getchannel("A").getextrema()[0] < 255
    return im.mode == "P" and "transparency" in im.info


def _processar_raster(dados, maximo, formato):
    try:
        Image.open(io.BytesIO(dados)).verify()
        im = Image.open(io.BytesIO(dados))
    except Image.DecompressionBombError:
        raise UploadError("A imagem tem dimensões grandes demais.")
    except Exception:
        raise UploadError("Arquivo inválido: não é uma imagem PNG, JPG ou WEBP.")
    if im.format not in FORMATOS_OK:
        raise UploadError(f"Formato não suportado ({im.format or 'desconhecido'}). Use PNG, JPG ou WEBP.")
    if im.width * im.height > IMG_MAX_PIXELS:
        raise UploadError("A imagem tem dimensões grandes demais.")
    with _semaforo():
        try:
            if getattr(im, "n_frames", 1) > 1:
                im.seek(0)
            if im.format == "JPEG":
                im.draft("RGB", (maximo, maximo))
            im = ImageOps.exif_transpose(im)
            if im.mode == "P":
                im = im.convert("RGBA")
            transparente = _tem_transparencia(im)
            im.thumbnail((maximo, maximo), Image.LANCZOS)
        except Image.DecompressionBombError:
            raise UploadError("A imagem tem dimensões grandes demais.")
        except Exception:
            raise UploadError("Não foi possível ler a imagem. O arquivo pode estar corrompido.")
        if formato == "auto":
            formato = "png" if transparente else "webp"
        buf = io.BytesIO()
        if formato == "png":
            im.convert("RGBA" if transparente else "RGB").save(buf, "PNG", optimize=True)
            ext = "png"
        elif formato == "jpg":
            if transparente:
                rgba = im.convert("RGBA")
                fundo = Image.new("RGB", im.size, (255, 255, 255))
                fundo.paste(rgba, mask=rgba.getchannel("A"))
                im = fundo
            im.convert("RGB").save(buf, "JPEG", quality=88, optimize=True, progressive=True)
            ext = "jpg"
        else:
            im.convert("RGBA" if transparente else "RGB").save(buf, "WEBP", quality=86, method=4)
            ext = "webp"
    return buf.getvalue(), ext


def _gravar(codigo, base, ext, conteudo):
    pasta = pasta_tenant(codigo)
    nome = f"{base}.{ext}"
    destino = os.path.join(pasta, nome)
    tmp = destino + f".{uuid.uuid4().hex[:6]}.tmp"
    try:
        os.makedirs(pasta, exist_ok=True)
        with open(tmp, "wb") as fh:
            fh.write(conteudo)
        os.replace(tmp, destino)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        if isinstance(e, PermissionError) or e.errno in (errno.EACCES, errno.EROFS, errno.EPERM):
            raise UploadError("Sem permissão de escrita no volume de imagens. Avise o suporte (o volume /data "
                              "precisa estar montado e gravável).")
        if e.errno == errno.ENOSPC:
            raise UploadError("O armazenamento do servidor está cheio. Avise o suporte.")
        raise UploadError("Não foi possível gravar a imagem no servidor. Tente novamente.")
    for outra in ("png", "jpg", "webp", "svg"):
        if outra != ext:
            try:
                os.remove(os.path.join(pasta, f"{base}.{outra}"))
            except OSError:
                pass
    _limpar_variantes(codigo, base)
    invalidar_indice(codigo)
    return nome


def _processar_storage(dados, cfg):
    if b"<svg" in dados[:4096].lower():
        return _processar_svg(dados, cfg.get("svg", False))
    return _processar_raster(dados, cfg.get("max", 1600), cfg.get("formato", "auto"))


def salvar_slot(codigo, slot, cfg, arquivo):
    conteudo, ext = _processar_storage(_ler_dados(arquivo), cfg)
    return _gravar(codigo, slot, ext, conteudo)


def salvar_galeria(codigo, arquivo, maximo=1600):
    """Imagem avulsa (galeria livre, corpo do blog, capa). SVG não é aceito aqui."""
    conteudo, ext = _processar_storage(_ler_dados(arquivo), {"max": maximo, "svg": False, "formato": "auto"})
    orig = os.path.splitext(getattr(arquivo, "filename", "") or "imagem")[0]
    return _gravar(codigo, f"{PREFIXO_GALERIA}{uuid.uuid4().hex[:8]}-{gerar_slug(orig, 40)}", ext, conteudo)


def remover(codigo, nome):
    caminho = caminho_seguro(codigo, nome)
    if not caminho:
        return False
    try:
        os.remove(caminho)
    except OSError:
        return False
    _limpar_variantes(codigo, nome.rsplit(".", 1)[0])
    invalidar_indice(codigo)
    return True


def remover_slot(codigo, slot):
    r = indice(codigo).get(slot)
    return remover(codigo, r[0]) if r else False


def excluir_pasta_tenant(codigo):
    shutil.rmtree(pasta_tenant(codigo), ignore_errors=True)
    invalidar_indice(codigo)


def snap_largura(w):
    if not w or w <= 0:
        return None
    return next((x for x in LARGURAS if x >= w), LARGURAS[-1])


def _limpar_variantes(codigo, stem):
    cache = os.path.join(pasta_tenant(codigo), ".cache")
    if not os.path.isdir(cache) or not re.match(r"^[a-z0-9_-]{1,90}$", stem):
        return
    for f in os.listdir(cache):
        if f.startswith(stem + "_"):
            try:
                os.remove(os.path.join(cache, f))
            except OSError:
                pass


def variante(codigo, nome, largura, aceita_webp):
    """(caminho, mime) na largura pedida, gerando/cacheando sob demanda. None se não existe."""
    origem = caminho_seguro(codigo, nome)
    if not origem or not os.path.isfile(origem):
        return None
    stem, ext = nome.rsplit(".", 1)
    if ext == "svg" or not largura:
        return origem, MIME[ext]
    saida_ext = "webp" if (aceita_webp or ext == "webp") else ext
    cache = os.path.join(pasta_tenant(codigo), ".cache")
    destino = os.path.join(cache, f"{stem}_{largura}.{saida_ext}")
    try:
        if os.path.isfile(destino) and os.path.getmtime(destino) >= os.path.getmtime(origem):
            return destino, MIME[saida_ext]
        with Image.open(origem) as im:
            if im.width <= largura and saida_ext == ext:
                return origem, MIME[ext]
            with _semaforo():
                im.load()
                if im.width > largura:
                    im = im.resize((largura, max(1, round(im.height * largura / im.width))), Image.LANCZOS)
                os.makedirs(cache, exist_ok=True)
                tmp = destino + f".{uuid.uuid4().hex[:6]}.tmp"
                if saida_ext == "webp":
                    im.save(tmp, "WEBP", quality=82, method=4)
                elif saida_ext == "png":
                    im.save(tmp, "PNG", optimize=True)
                else:
                    im.convert("RGB").save(tmp, "JPEG", quality=84, optimize=True, progressive=True)
                os.replace(tmp, destino)
        return destino, MIME[saida_ext]
    except Exception:
        current_app.logger.exception("Falha ao gerar variante de %s/%s", codigo, nome)
        return origem, MIME[ext]


def uso_disco(codigo):
    total = 0
    for raiz, _d, arquivos in os.walk(pasta_tenant(codigo)):
        for f in arquivos:
            try:
                total += os.path.getsize(os.path.join(raiz, f))
            except OSError:
                pass
    return total


def volume_info():
    """Diz se UPLOAD_DIR está num volume montado e gravável (senão as imagens somem a cada deploy)."""
    base = current_app.config["UPLOAD_DIR"]
    gravavel, erro = True, ""
    try:
        os.makedirs(base, exist_ok=True)
        teste = os.path.join(base, f".w{uuid.uuid4().hex[:6]}")
        with open(teste, "w") as fh:
            fh.write("ok")
        os.remove(teste)
    except OSError as e:
        gravavel, erro = False, str(e)
    p = os.path.abspath(base)
    while p != os.path.dirname(p):
        if os.path.ismount(p):
            return {"caminho": base, "montado": p, "gravavel": gravavel, "erro": erro}
        p = os.path.dirname(p)
    return {"caminho": base, "montado": None, "gravavel": gravavel, "erro": erro}


# ═══════════════════════════════════════════════════════════════════════════
# RATELIMIT — Rate limit persistente no banco (vale entre workers do gunicorn).
# ═══════════════════════════════════════════════════════════════════════════
def excedido(chave, maximo, janela):
    n = db.session.scalar(select(func.count(RateHit.id)).where(RateHit.chave == chave[:120],
                                                               RateHit.ts > time.time() - janela))
    return (n or 0) >= maximo


def registrar_ratelimit(chave):
    db.session.add(RateHit(chave=chave[:120], ts=time.time()))
    db.session.commit()
    if random.random() < 0.01:
        db.session.execute(delete(RateHit).where(RateHit.ts < time.time() - 86400))
        db.session.commit()


def permitir(chave, maximo, janela):
    if excedido(chave, maximo, janela):
        return False
    registrar_ratelimit(chave)
    return True


# ═══════════════════════════════════════════════════════════════════════════
# TENANCY — Identificação do tenant pelo Host (cache em memória com TTL curto).
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class TenantInfo:
    id: int
    codigo: str
    nome: str
    template: str
    whatsapp: str
    ativo: bool
    dominios: tuple

    @property
    def dominio_principal(self):
        return self.dominios[0] if self.dominios else None


_cache = {}
_lock_tenancy = threading.Lock()
_MAX = 5000
_TTL_NEG = 10


def _info(t):
    return TenantInfo(t.id, t.codigo, t.nome, t.template, t.whatsapp or "", bool(t.ativo), tuple(t.dominios_lista))


def _buscar(chave, consulta):
    hit = _cache.get(chave)
    if hit and hit[0] > time.time():
        return hit[1]
    t = db.session.scalars(consulta).first()
    info = _info(t) if t else None
    with _lock_tenancy:
        if len(_cache) > _MAX:
            _cache.clear()
        _cache[chave] = (time.time() + (current_app.config["TENANT_CACHE_TTL"] if info else _TTL_NEG), info)
    return info


def buscar_por_host(host):
    if not host:
        return None
    return _buscar("h:" + host, select(Tenant).join(TenantDominio).where(TenantDominio.dominio == host))


def buscar_por_codigo(codigo):
    if not CODIGO_RE.match(codigo or ""):
        return None
    return _buscar("c:" + codigo, select(Tenant).where(Tenant.codigo == codigo))


def limpar_cache_tenancy():
    """Vale para ESTE worker; nos outros expira pelo TTL (ver README)."""
    with _lock_tenancy:
        _cache.clear()


def resolver_contexto():
    """before_request: define g.tenant / g.admin_host ou responde 404 limpo."""
    g.tenant = None
    g.admin_host = False
    g.definir_cookie_tenant = None
    caminho = request.path
    if caminho in ("/healthz", "/_dominio_ok"):
        return None
    cfg = current_app.config
    host = normalizar_host(request.host)

    if cfg["ADMIN_HOST"] and host == cfg["ADMIN_HOST"]:
        g.admin_host = True
        if caminho.startswith(("/_admin", "/static/", "/m/")):
            return None
        if caminho == "/":
            return redirect("/_admin/")
        abort(404)

    tenant = None
    if cfg["ALLOW_TENANT_OVERRIDE"]:                      # só testes: ?tenant= / X-Tenant / cookie
        explicito = (request.args.get("tenant") or request.headers.get("X-Tenant") or "").strip().lower()
        if explicito:
            tenant = buscar_por_codigo(explicito)
            if tenant:
                g.definir_cookie_tenant = explicito
    if tenant is None:
        tenant = buscar_por_host(host)
    if tenant is None and cfg["ALLOW_TENANT_OVERRIDE"]:
        tenant = buscar_por_codigo((request.cookies.get("_tenant") or "").strip().lower())
    if tenant is None or not tenant.ativo or caminho.startswith("/_admin"):
        abort(404)
    g.tenant = tenant
    return None


def aplicar_cookie_tenant(resp):
    cod = g.get("definir_cookie_tenant")
    if cod:
        resp.set_cookie("_tenant", cod, httponly=True, samesite="Lax")
    return resp


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS — Funções dos templates: img(), img_url(), img_srcset(), site_static()... e consultas do blog.
# ═══════════════════════════════════════════════════════════════════════════
EXTS_FALLBACK = ("webp", "png", "jpg", "jpeg", "svg")


def base_url():
    t = g.get("tenant")
    host = normalizar_host(request.host)
    if t and t.dominio_principal and host in t.dominios:
        porta = request.host.partition(":")[2]
        return f"{request.scheme}://{t.dominio_principal}" + (f":{porta}" if porta else "")
    return request.host_url.rstrip("/")


def canonical_url():
    return base_url() + request.path


def _fallback_estatico(template, slot):
    pasta = os.path.join(current_app.config["SITES_DIR"], template, "static")
    for ext in EXTS_FALLBACK:
        p = os.path.join(pasta, f"{slot}.{ext}")
        if os.path.isfile(p):
            return f"{slot}.{ext}", int(os.path.getmtime(p))
    return None


def tem_img(slot):
    """True se o cliente já enviou imagem para o slot (não conta o fallback)."""
    t = g.get("tenant")
    return bool(t and slot in indice(t.codigo))


def img_url(slot, w=None):
    """URL do slot do tenant atual. Sem upload → fallback em sites/<template>/static/ → placeholder SVG."""
    t = g.get("tenant")
    if t is None:
        return url_for("site.placeholder", t=slot)
    r = indice(t.codigo).get(slot)
    if r:
        args = {"v": r[1]}
        if not r[0].endswith(".svg"):
            largura = snap_largura(w)
            if largura:
                args["w"] = largura
        return url_for("site.media", codigo=t.codigo, arquivo=r[0], **args)
    fb = _fallback_estatico(t.template, slot) if SLOT_RE.match(slot or "") else None
    if fb:
        return url_for("site.site_static", arquivo=fb[0], v=fb[1])
    s = obter(t.template)
    nome = (s.slots.get(slot, {}).get("nome") if s else None) or slot
    return url_for("site.placeholder", t=nome)


def img(slot):
    return img_url(slot)


def img_abs(slot, w=None):
    return base_url() + img_url(slot, w)


def img_srcset(slot, larguras=(400, 800, 1200, 1600)):
    t = g.get("tenant")
    r = indice(t.codigo).get(slot) if t else None
    if not r or r[0].endswith(".svg"):
        return ""
    return ", ".join(f"{url_for('site.media', codigo=t.codigo, arquivo=r[0], w=w, v=r[1])} {w}w" for w in larguras)


def site_static_helpers(arquivo):
    t = g.get("tenant")
    if t is None:
        return ""
    p = os.path.join(current_app.config["SITES_DIR"], t.template, "static", arquivo)
    return url_for("site.site_static", arquivo=arquivo, **({"v": int(os.path.getmtime(p))} if os.path.isfile(p) else {}))


def capa_url(post, w=None):
    t = g.get("tenant")
    if not post or not post.capa or t is None:
        return ""
    r = indice(t.codigo).get(post.capa.rsplit(".", 1)[0])
    if not r or r[0] != post.capa:
        return ""
    args = {"v": r[1]}
    largura = snap_largura(w)
    if largura and not post.capa.endswith(".svg"):
        args["w"] = largura
    return url_for("site.media", codigo=t.codigo, arquivo=post.capa, **args)


def stmt_publicados(tenant_id):
    """Só posts publicados e já na data. SEMPRE por tenant."""
    return (select(Post).where(Post.tenant_id == tenant_id, Post.status == "publicado",
                               Post.publicado_em <= agora_utc())
            .order_by(Post.publicado_em.desc(), Post.id.desc()))


def ultimos_posts(n=3):
    t = g.get("tenant")
    return db.session.scalars(stmt_publicados(t.id).limit(n)).all() if t else []


def whatsapp_link(texto=""):
    t = g.get("tenant")
    return wa_link(t.whatsapp, texto) if t and t.whatsapp else ""


def registrar_helpers(app):
    @app.context_processor
    def _contexto():
        return {"tenant": g.get("tenant"), "csrf_token": csrf_token, "img": img, "img_url": img_url,
                "img_abs": img_abs, "img_srcset": img_srcset, "tem_img": tem_img, "site_static": site_static_helpers,
                "capa_url": capa_url, "ultimos_posts": ultimos_posts, "whatsapp_link": whatsapp_link,
                "base_url": base_url, "canonical_url": canonical_url}

    app.add_template_filter(filtro_data_br, "data_br")
    app.add_template_filter(filtro_tempo, "tempo")
    app.add_template_filter(fmt_bytes, "bytes_h")
    app.add_template_filter(lambda v: normalizar_whatsapp(v) or "", "wa_numero")


# ═══════════════════════════════════════════════════════════════════════════
# SERVICES — Regras de negócio de tenants (super-admin e manage.py).
# ═══════════════════════════════════════════════════════════════════════════
class ErroNegocio(ValueError):
    """Mensagem em português, segura para mostrar."""


def gerar_senha():
    return secrets.token_urlsafe(9)


def validar_senha_nova(senha):
    if not senha or len(senha) < 8:
        raise ErroNegocio("A senha deve ter pelo menos 8 caracteres.")
    if len(senha) > 128:
        raise ErroNegocio("A senha é longa demais (máximo 128 caracteres).")


def validar_codigo(codigo):
    codigo = (codigo or "").strip().lower()
    if not CODIGO_RE.match(codigo) or not (3 <= len(codigo) <= 40):
        raise ErroNegocio("Código inválido. Use lp + letras minúsculas, números ou _ (ex.: lpjulia).")
    return codigo


def validar_template(template):
    template = (template or "").strip()
    if not obter(template):
        raise ErroNegocio(f"Template '{template}' não encontrado em sites/ (precisa ter site.py).")
    return template


def normalizar_dominios(entrada):
    if isinstance(entrada, str):
        entrada = re.split(r"[\s,;]+", entrada)
    saida = []
    for bruto in entrada:
        bruto = (bruto or "").strip()
        if not bruto:
            continue
        d = normalizar_host(re.sub(r"^https?://", "", bruto.lower()))
        if not DOMINIO_RE.match(d):
            raise ErroNegocio(f"Domínio inválido: {bruto[:80]}")
        if d == current_app.config["ADMIN_HOST"]:
            raise ErroNegocio(f"{d} é o host mestre (ADMIN_HOST) e não pode ser de um cliente.")
        if d not in saida:
            saida.append(d)
    return saida


def _checar_dominios_livres(dominios, tenant_id=None):
    for d in dominios:
        dono = db.session.scalars(select(TenantDominio).where(TenantDominio.dominio == d)).first()
        if dono and dono.tenant_id != tenant_id:
            raise ErroNegocio(f"O domínio {d} já está cadastrado em outro cliente.")


def _campos_opcionais(email_aviso, whatsapp, webhook_url):
    email_aviso = sanitize_input(email_aviso).lower()
    if email_aviso and not eh_email(email_aviso):
        raise ErroNegocio("E-mail de aviso inválido.")
    wa = normalizar_whatsapp(whatsapp)
    if wa is None:
        raise ErroNegocio("WhatsApp inválido. Informe DDD + número (ex.: 11 99999-9999).")
    webhook_url = (webhook_url or "").strip()
    if webhook_url:
        p = urlparse(webhook_url)
        if p.scheme not in ("http", "https") or not p.netloc or len(webhook_url) > 500:
            raise ErroNegocio("URL de webhook inválida (use http:// ou https://).")
    return email_aviso or None, wa or None, webhook_url or None


def definir_dominios(tenant, dominios):
    _checar_dominios_livres(dominios, tenant.id)
    atuais = {d.dominio: d for d in tenant.dominios}
    for d, obj in atuais.items():
        if d not in dominios:
            tenant.dominios.remove(obj)
    db.session.flush()
    for d in dominios:
        if d not in atuais:
            tenant.dominios.append(TenantDominio(dominio=d))


def criar_tenant(codigo, nome, dominios, template, senha=None, email_aviso=None, whatsapp=None, webhook_url=None):
    codigo = validar_codigo(codigo)
    nome = sanitize_input(nome)[:120]
    if not nome:
        raise ErroNegocio("Informe o nome do cliente.")
    template = validar_template(template)
    doms = normalizar_dominios(dominios)
    if not doms:
        raise ErroNegocio("Informe pelo menos um domínio.")
    if db.session.scalars(select(Tenant).where(Tenant.codigo == codigo)).first():
        raise ErroNegocio(f"O código {codigo} já existe.")
    _checar_dominios_livres(doms)
    email_aviso, whatsapp, webhook_url = _campos_opcionais(email_aviso, whatsapp, webhook_url)
    if senha:
        validar_senha_nova(senha)
    else:
        senha = gerar_senha()
    t = Tenant(codigo=codigo, nome=nome, template=template, senha_hash=generate_password_hash(senha),
               email_aviso=email_aviso, whatsapp=whatsapp, webhook_url=webhook_url, ativo=True)
    t.dominios = [TenantDominio(dominio=d) for d in doms]
    db.session.add(t)
    db.session.commit()
    try:
        os.makedirs(pasta_tenant(codigo), exist_ok=True)
    except OSError:
        current_app.logger.warning("Não foi possível criar a pasta de uploads de %s", codigo)
    limpar_cache_tenancy()
    return t, senha


def atualizar_tenant(t, nome, dominios, template, email_aviso, whatsapp, webhook_url, ativo):
    nome = sanitize_input(nome)[:120]
    if not nome:
        raise ErroNegocio("Informe o nome do cliente.")
    template = validar_template(template)
    doms = normalizar_dominios(dominios)
    if not doms:
        raise ErroNegocio("Informe pelo menos um domínio.")
    email_aviso, whatsapp, webhook_url = _campos_opcionais(email_aviso, whatsapp, webhook_url)
    t.nome, t.template, t.ativo = nome, template, bool(ativo)
    t.email_aviso, t.whatsapp, t.webhook_url = email_aviso, whatsapp, webhook_url
    definir_dominios(t, doms)
    db.session.commit()
    limpar_cache_tenancy()


def resetar_senha_services(t, senha=None):
    if senha:
        validar_senha_nova(senha)
    else:
        senha = gerar_senha()
    t.senha_hash = generate_password_hash(senha)
    t.sessao_versao = (t.sessao_versao or 1) + 1
    db.session.commit()
    return senha


def trocar_senha_propria(t, atual, nova, confirmar):
    if not check_password_hash(t.senha_hash, atual or ""):
        raise ErroNegocio("A senha atual não confere.")
    if nova != confirmar:
        raise ErroNegocio("A confirmação não é igual à nova senha.")
    validar_senha_nova(nova)
    t.senha_hash = generate_password_hash(nova)
    t.sessao_versao = (t.sessao_versao or 1) + 1
    db.session.commit()


def excluir_tenant(t):
    codigo, tid = t.codigo, t.id
    db.session.execute(delete(Lead).where(Lead.tenant_id == tid))
    db.session.execute(delete(Post).where(Post.tenant_id == tid))
    db.session.delete(t)
    db.session.commit()
    excluir_pasta_tenant(codigo)
    limpar_cache_tenancy()


# ═══════════════════════════════════════════════════════════════════════════
# LEADSVC — Consulta, filtros e CSV de leads (painel do cliente e super-admin).
# ═══════════════════════════════════════════════════════════════════════════
POR_PAGINA = 20


def consulta(tenant_id, q="", de="", ate="", status=""):
    """SEMPRE filtra por tenant_id."""
    stmt = select(Lead).where(Lead.tenant_id == tenant_id)
    q = (q or "").strip()[:100]
    if q:
        like = f"%{escape_like(q)}%"
        conds = [Lead.nome.ilike(like, escape="\\"), Lead.email.ilike(like, escape="\\"),
                 Lead.mensagem.ilike(like, escape="\\"), Lead.telefone.ilike(like, escape="\\")]
        dig = so_digitos(q)
        if len(dig) >= 4:
            conds.append(Lead.telefone.like(f"%{dig}%"))
        stmt = stmt.where(or_(*conds))
    d1, d2 = parse_data(de), parse_data(ate)
    if d1:
        stmt = stmt.where(Lead.criado_em >= dia_local_para_utc(d1))
    if d2:
        stmt = stmt.where(Lead.criado_em <= dia_local_para_utc(d2, fim=True))
    if status == "novos":
        stmt = stmt.where(Lead.atendido.is_(False))
    elif status == "atendidos":
        stmt = stmt.where(Lead.atendido.is_(True))
    return stmt.order_by(Lead.criado_em.desc(), Lead.id.desc())


def paginar(stmt, pagina):
    return db.paginate(stmt, page=max(1, pagina or 1), per_page=POR_PAGINA, error_out=False)


def _csv_seguro(v):
    """Evita injeção de fórmulas no Excel/Planilhas."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


COLUNAS = ["id", "data", "nome", "telefone", "email", "mensagem", "atendido", "utm_source", "utm_medium",
           "utm_campaign", "utm_term", "utm_content", "referrer", "pagina", "extras"]


def gerar_csv(tenant_id, **filtros):
    """CSV com BOM e separador ';' (Excel pt-BR). Limite de 50 mil linhas."""
    buf = io.StringIO()
    buf.write("\ufeff")
    w = csv.writer(buf, delimiter=";")
    w.writerow(COLUNAS)
    for l in db.session.scalars(consulta(tenant_id, **filtros).limit(50000)):
        w.writerow([l.id, para_local(l.criado_em).strftime("%d/%m/%Y %H:%M"), _csv_seguro(l.nome),
                    _csv_seguro(l.telefone), _csv_seguro(l.email), _csv_seguro(l.mensagem),
                    "sim" if l.atendido else "não", _csv_seguro(l.utm_source), _csv_seguro(l.utm_medium),
                    _csv_seguro(l.utm_campaign), _csv_seguro(l.utm_term), _csv_seguro(l.utm_content),
                    _csv_seguro(l.referrer), _csv_seguro(l.pagina),
                    _csv_seguro(json.dumps(l.extras, ensure_ascii=False) if l.extras else "")])
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════════════════
# NOTIFY — Notificações de lead (e-mail SMTP, webhook). Falha de notificação NUNCA perde o lead nem quebra a resposta:
# ═══════════════════════════════════════════════════════════════════════════
log_notify = logging.getLogger(__name__)


def smtp_configurado(cfg=None):
    cfg = cfg or current_app.config
    return bool(cfg["SMTP_HOST"] and (cfg["SMTP_FROM"] or cfg["SMTP_USER"]))


def enviar_email(cfg, para, assunto, texto):
    msg = EmailMessage()
    msg["Subject"] = " ".join(str(assunto).split())[:200]
    msg["From"] = cfg["SMTP_FROM"] or cfg["SMTP_USER"]
    msg["To"] = para
    msg.set_content(texto)
    modo = cfg["SMTP_SECURITY"]
    cls = smtplib.SMTP_SSL if modo == "ssl" else smtplib.SMTP
    with cls(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15) as srv:
        if modo == "starttls":
            srv.starttls()
        if cfg["SMTP_USER"]:
            srv.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
        srv.send_message(msg)


def _texto_lead(tenant, lead):
    linhas = [f"Novo lead em {tenant.nome}", "", f"Nome: {lead.nome or '-'}", f"Telefone: {lead.telefone or '-'}",
              f"E-mail: {lead.email or '-'}"]
    if lead.mensagem:
        linhas += ["", "Mensagem:", lead.mensagem]
    if lead.extras:
        linhas += ["", "Outros campos:"] + [f"- {k}: {v}" for k, v in lead.extras.items()]
    origem = [x for x in (lead.utm_source, lead.utm_medium, lead.utm_campaign) if x]
    if origem or lead.referrer:
        linhas += ["", "Origem: " + (" / ".join(origem) or "-"), f"Referrer: {lead.referrer or '-'}"]
    if lead.telefone:
        link = wa_link(lead.telefone, f"Olá {lead.nome or ''}, recebi seu contato pelo site!")
        if link:
            linhas += ["", f"Responder pelo WhatsApp: {link}"]
    return "\n".join(linhas)


def _payload(tenant, lead):
    return {"evento": "lead.criado", "tenant": {"codigo": tenant.codigo, "nome": tenant.nome},
            "lead": {"id": lead.id, "nome": lead.nome, "telefone": lead.telefone, "email": lead.email,
                     "mensagem": lead.mensagem, "extras": lead.extras or {}, "utm_source": lead.utm_source,
                     "utm_medium": lead.utm_medium, "utm_campaign": lead.utm_campaign, "utm_term": lead.utm_term,
                     "utm_content": lead.utm_content, "referrer": lead.referrer, "pagina": lead.pagina,
                     "criado_em": lead.criado_em.isoformat() + "Z"}}


def _processar_notify(app, lead_id):
    with app.app_context():
        try:
            lead = db.session.get(Lead, lead_id)
            tenant = db.session.get(Tenant, lead.tenant_id) if lead else None
            if not lead or not tenant:
                return
            cfg = app.config
            if tenant.email_aviso and smtp_configurado(cfg):
                try:
                    enviar_email(cfg, tenant.email_aviso, f"Novo lead: {lead.nome or 'sem nome'} — {tenant.nome}",
                                 _texto_lead(tenant, lead))
                    lead.notif_email = "ok"
                except Exception as e:
                    lead.notif_email = f"erro:{type(e).__name__}"
                    log_notify.warning("Falha no e-mail do lead %s: %s", lead_id, type(e).__name__)
            else:
                lead.notif_email = "nao_configurado"
            if tenant.webhook_url:
                try:
                    r = requests.post(tenant.webhook_url, data=json.dumps(_payload(tenant, lead)),
                                      headers={"Content-Type": "application/json", "User-Agent": "lp-platform/1.0"},
                                      timeout=8)
                    lead.notif_webhook = "ok" if r.status_code < 300 else f"erro:http{r.status_code}"
                except Exception as e:
                    lead.notif_webhook = f"erro:{type(e).__name__}"
                    log_notify.warning("Falha no webhook do lead %s: %s", lead_id, type(e).__name__)
            else:
                lead.notif_webhook = "nao_configurado"
            db.session.commit()
        except Exception:
            db.session.rollback()
            log_notify.exception("Erro ao notificar lead %s", lead_id)


def notificar_lead_async(lead_id):
    app = current_app._get_current_object()
    threading.Thread(target=_processar_notify, args=(app, lead_id), daemon=True, name=f"notif-lead-{lead_id}").start()


# ═══════════════════════════════════════════════════════════════════════════
# BLUEPRINTS.SITE — Rotas públicas do tenant: home da LP, blog, feed, sitemap, robots, mídia e verificação de domínio.
# ═══════════════════════════════════════════════════════════════════════════
bp_site = Blueprint("site", __name__)
SLUG_RE = re.compile(r"^[a-z0-9-]{1,120}$")
POSTS_POR_PAGINA = 9
CSP_SVG = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


@bp_site.get("/healthz")
def healthz():
    db.session.execute(text("SELECT 1"))
    return Response("ok", mimetype="text/plain")


@bp_site.get("/_dominio_ok")
def dominio_ok():
    """`ask` do on_demand_tls do Caddy: 200 SOMENTE se o domínio (ou www.) é de um tenant ativo."""
    # O Caddy consulta direto (sem X-Forwarded-For). Pedidos vindos pelo proxy público são recusados.
    if not current_app.config["DOMINIO_OK_PUBLICO"] and request.headers.get("X-Forwarded-For"):
        abort(404)
    dominio = normalizar_host(request.args.get("domain", ""))
    if not DOMINIO_RE.match(dominio):
        abort(404)
    admin = current_app.config["ADMIN_HOST"]
    if admin and dominio == admin:
        return Response("ok", mimetype="text/plain")
    existe = db.session.scalar(select(Tenant.id).join(TenantDominio)
                               .where(TenantDominio.dominio == dominio, Tenant.ativo.is_(True)))
    if not existe:
        abort(404)
    return Response("ok", mimetype="text/plain")


def _render_site(nome, **ctx):
    return render_template(f"sites/{g.tenant.template}/{nome}", **ctx)


def _render_blog(nome_site, nome_base, **ctx):
    """Ordem: sites/<lp>/blog_post.html → templates/base_blog_post.html."""
    if arquivo_existe(g.tenant.template, nome_site):
        return _render_site(nome_site, **ctx)
    return render_template(nome_base, **ctx)


@bp_site.get("/")
def index():
    if not arquivo_existe(g.tenant.template, "index.html"):
        current_app.logger.error("Template %s sem index.html (tenant %s)", g.tenant.template, g.tenant.codigo)
        return render_template("erro.html", codigo=503, mensagem="Site temporariamente indisponível."), 503
    return _render_site("index.html", posts_recentes=ultimos_posts(3), blog_url=url_for("site.blog_lista"))


@bp_site.get("/obrigado")
def obrigado():
    if arquivo_existe(g.tenant.template, "obrigado.html"):
        return _render_site("obrigado.html")
    return render_template("obrigado.html")


@bp_site.get("/blog", endpoint="blog_lista")
def blog_lista_site():
    pagina = request.args.get("pagina", 1, type=int) or 1
    pag = db.paginate(stmt_publicados(g.tenant.id), page=max(1, pagina), per_page=POSTS_POR_PAGINA, error_out=False)
    return _render_blog("blog_index.html", "base_blog_index.html", posts=pag.items, pag=pag,
                        meta_title=f"Blog — {g.tenant.nome}", meta_description=f"Artigos e novidades de {g.tenant.nome}.")


@bp_site.get("/blog/feed.xml")
def blog_feed():
    base = base_url()
    itens = []
    for p in db.session.scalars(stmt_publicados(g.tenant.id).limit(30)):
        link = f"{base}/blog/{p.slug}"
        desc = p.resumo or p.meta_description or texto_puro(p.corpo_html, 200)
        itens.append(f"<item><title>{xml_escape(p.titulo)}</title><link>{xml_escape(link)}</link>"
                     f"<guid isPermaLink=\"true\">{xml_escape(link)}</guid>"
                     f"<pubDate>{format_datetime(p.publicado_em.replace(tzinfo=timezone.utc))}</pubDate>"
                     f"<description>{xml_escape(desc)}</description></item>")
    xml = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><rss version=\"2.0\"><channel>"
           f"<title>{xml_escape(g.tenant.nome)} — Blog</title><link>{xml_escape(base)}/blog</link>"
           f"<description>Novidades de {xml_escape(g.tenant.nome)}</description><language>pt-BR</language>"
           + "".join(itens) + "</channel></rss>")
    return Response(xml, mimetype="application/rss+xml")


@bp_site.get("/blog/<slug>")
def blog_post(slug):
    if not SLUG_RE.match(slug):
        abort(404)
    post = db.session.scalars(stmt_publicados(g.tenant.id).where(Post.slug == slug)).first()
    if not post:
        abort(404)
    relacionados = db.session.scalars(stmt_publicados(g.tenant.id).where(Post.id != post.id).limit(3)).all()
    descricao = post.meta_description or post.resumo or texto_puro(post.corpo_html)
    capa = capa_url(post, 1200)
    og = (base_url() + capa) if capa else (img_abs("og") if tem_img("og") else "")
    jsonld = {"@context": "https://schema.org", "@type": "BlogPosting", "headline": post.titulo,
              "description": descricao, "datePublished": post.publicado_em.isoformat() + "Z",
              "dateModified": post.atualizado_em.isoformat() + "Z", "mainEntityOfPage": canonical_url(),
              "publisher": {"@type": "Organization", "name": g.tenant.nome}}
    if og:
        jsonld["image"] = og
    return _render_blog("blog_post.html", "base_blog_post.html", post=post, relacionados=relacionados,
                        meta_title=f"{post.titulo} — {g.tenant.nome}", meta_description=descricao,
                        meta_og_image=og, jsonld=jsonld)


@bp_site.get("/sitemap.xml")
def sitemap():
    base = base_url()
    urls = [(f"{base}/", None), (f"{base}/blog", None)]
    for p in db.session.scalars(stmt_publicados(g.tenant.id).limit(5000)):
        urls.append((f"{base}/blog/{p.slug}", p.atualizado_em))
    corpo = "".join(f"<url><loc>{xml_escape(u)}</loc>" + (f"<lastmod>{d.strftime('%Y-%m-%d')}</lastmod>" if d else "")
                    + "</url>" for u, d in urls)
    return Response("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                    f"<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">{corpo}</urlset>",
                    mimetype="application/xml")


@bp_site.get("/robots.txt")
def robots():
    return Response(f"User-agent: *\nAllow: /\nDisallow: /painel\nDisallow: /lead\n\nSitemap: {base_url()}/sitemap.xml\n",
                    mimetype="text/plain")


@bp_site.get("/m/<codigo>/<arquivo>")
def media(codigo, arquivo):
    """Imagens do tenant. Valida nome, impede path traversal e impede um tenant de ler a pasta de outro."""
    if not CODIGO_RE.match(codigo) or not NOME_RE.match(arquivo):
        abort(404)
    if not g.admin_host and (g.tenant is None or g.tenant.codigo != codigo):
        abort(404)
    largura = snap_largura(request.args.get("w", type=int))
    r = variante(codigo, arquivo, largura, "image/webp" in request.headers.get("Accept", ""))
    if not r:
        abort(404)
    resp = send_file(r[0], mimetype=r[1], conditional=True)
    resp.headers["Cache-Control"] = ("public, max-age=31536000, immutable" if request.args.get("v")
                                     else "public, max-age=3600")
    resp.headers["Vary"] = "Accept"
    if arquivo.endswith(".svg"):
        resp.headers["Content-Security-Policy"] = CSP_SVG
    return resp


@bp_site.get("/s/<path:arquivo>", endpoint="site_static")
def site_static_site(arquivo):
    """Arquivos de sites/<template>/static/ — só do template do tenant atual."""
    if g.tenant is None or not TEMPLATE_RE.match(g.tenant.template):
        abort(404)
    pasta = os.path.join(current_app.config["SITES_DIR"], g.tenant.template, "static")
    resp = send_from_directory(pasta, arquivo, conditional=True)       # bloqueia path traversal
    resp.headers["Cache-Control"] = ("public, max-age=31536000, immutable" if request.args.get("v")
                                     else "public, max-age=3600")
    if arquivo.lower().endswith(".svg"):
        resp.headers["Content-Security-Policy"] = CSP_SVG
    return resp


@bp_site.get("/placeholder.svg")
def placeholder():
    texto = escape(sanitize_input(request.args.get("t", "Imagem"))[:40] or "Imagem")
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 450" preserveAspectRatio="xMidYMid slice">'
           '<rect width="800" height="450" fill="#e2e8f0"/><path d="M300 280l70-80 50 55 35-40 75 65z" fill="#cbd5e1"/>'
           '<circle cx="350" cy="170" r="22" fill="#cbd5e1"/>'
           f'<text x="400" y="350" text-anchor="middle" font-family="sans-serif" font-size="26" fill="#64748b">{texto}</text></svg>')
    return Response(svg, mimetype="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400", "Content-Security-Policy": CSP_SVG})


@bp_site.get("/favicon.ico")
def favicon():
    if g.tenant is None:
        abort(404)
    r = indice(g.tenant.codigo).get("favicon")
    if r:
        return redirect(url_for("site.media", codigo=g.tenant.codigo, arquivo=r[0], v=r[1]), code=302)
    pasta = os.path.join(current_app.config["SITES_DIR"], g.tenant.template, "static")
    for nome in ("favicon.ico", "favicon.svg", "favicon.png"):
        if os.path.isfile(os.path.join(pasta, nome)):
            return redirect(url_for("site.site_static", arquivo=nome), code=302)
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#334155"/></svg>'
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


# ═══════════════════════════════════════════════════════════════════════════
# BLUEPRINTS.API — Captura de leads: POST /lead (form → redirect /obrigado) e POST /api/lead (JSON ou form), por tenant.
# ═══════════════════════════════════════════════════════════════════════════
bp_api = Blueprint("api", __name__)

UA_BLOQUEADOS = ("python-requests", "python-urllib", "urllib", "curl", "wget", "postmanruntime", "httpx",
                 "go-http-client", "scrapy", "libwww", "okhttp", "spider", "crawler")
BOTS_PERMITIDOS = ("googlebot", "bingbot", "yandexbot", "duckduckbot", "baiduspider", "facebookexternalhit")
CAMPOS_TELEFONE = ("telefone", "whatsapp", "phone", "tel", "celular")
CAMPOS_MENSAGEM = ("mensagem", "message", "msg")
RESERVADOS = {"nome", "name", "email", "e-mail", "website", "_hp", "_next", "_csrf", "referrer", "pagina", "tenant",
              *CAMPOS_TELEFONE, *CAMPOS_MENSAGEM}


def _bot_obvio(ua):
    ua = (ua or "").lower()
    if any(b in ua for b in BOTS_PERMITIDOS):
        return False
    return not ua or any(b in ua for b in UA_BLOQUEADOS)


def _achatar(v, limite=1000):
    if isinstance(v, (list, tuple)):
        v = ", ".join(_achatar(x, limite) for x in v)
    elif isinstance(v, dict):
        v = json.dumps(v, ensure_ascii=False)
    return sanitize_input(v)[:limite]


def _dados():
    if request.is_json:
        d = request.get_json(silent=True)
        return d if isinstance(d, dict) else {}
    d = request.form.to_dict(flat=False)
    return {k: (v[0] if len(v) == 1 else v) for k, v in d.items()}


def _primeiro(d, nomes, limite=1000):
    for n in nomes:
        if d.get(n):
            return _achatar(d[n], limite)
    return ""


def _responder(ok, mensagem, status=200, json_=True, proximo=None, **extra):
    if json_:
        return jsonify(ok=ok, **({"mensagem": mensagem} if ok else {"erro": mensagem}), **extra), status
    if ok:
        return redirect(safe_next(proximo) or url_for("site.obrigado"), code=303)
    return render_template("lead_erro.html", mensagem=mensagem), status


@bp_api.route("/lead", methods=["POST"], endpoint="lead")
@bp_api.route("/api/lead", methods=["POST"], endpoint="lead_api")
def lead():
    tenant = g.get("tenant")
    if tenant is None:
        return jsonify(ok=False, erro="Não encontrado."), 404
    cfg = current_app.config
    d = _dados()
    quer_json = (request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest"
                 or request.accept_mimetypes.best == "application/json")
    proximo = _achatar(d.get("_next"), 300) if d.get("_next") else None

    # 1) Honeypot: sucesso falso, sem gravar
    if d.get("website") or d.get("_hp"):
        return _responder(True, "Recebemos seu contato!", json_=quer_json, proximo=proximo,
                          redirect=url_for("site.obrigado"))
    # 2) Bots óbvios (não bloqueia Googlebot/Bing)
    if cfg["BLOCK_BOTS"] and _bot_obvio(request.headers.get("User-Agent")):
        return _responder(False, "Requisição não permitida.", 403, json_=quer_json)
    # 3) Rate limit por IP + tenant (persistente)
    if not permitir(f"lead:{tenant.id}:{request.remote_addr}", cfg["LEAD_RATE_MAX"], cfg["LEAD_RATE_JANELA"]):
        return _responder(False, "Muitas tentativas. Aguarde alguns minutos e tente novamente.", 429, json_=quer_json)
    # 4) Validação
    nome = _primeiro(d, ("nome", "name"), 120)
    telefone = _primeiro(d, CAMPOS_TELEFONE, 30)
    email = _primeiro(d, ("email", "e-mail"), 254).lower()
    mensagem = _primeiro(d, CAMPOS_MENSAGEM, 3000)
    if not nome:
        return _responder(False, "Informe seu nome.", 422, json_=quer_json)
    if not telefone and not email:
        return _responder(False, "Informe um telefone/WhatsApp ou e-mail para contato.", 422, json_=quer_json)
    if telefone and not telefone_valido(telefone):
        return _responder(False, "Telefone inválido. Use DDD + número (ex.: 11 99999-9999).", 422, json_=quer_json)
    if email and not eh_email(email):
        return _responder(False, "E-mail inválido.", 422, json_=quer_json)
    # 5) Extras livres (JSON) + origem
    extras = {}
    for k, v in d.items():
        if k in RESERVADOS or k.startswith(("utm_", "_")) or len(extras) >= 20:
            continue
        chave = sanitize_input(k)[:60]
        if chave and re.match(r"^[\w .\-/]+$", chave, re.U):
            extras[chave] = _achatar(v)

    def utm(n):
        return _achatar(d.get(n), 200) or None

    lead_ = Lead(tenant_id=tenant.id, nome=nome, telefone=telefone or None, email=email or None,
                 mensagem=mensagem or None, extras=extras or None, utm_source=utm("utm_source"),
                 utm_medium=utm("utm_medium"), utm_campaign=utm("utm_campaign"), utm_term=utm("utm_term"),
                 utm_content=utm("utm_content"),
                 referrer=(_achatar(d.get("referrer"), 500) or sanitize_input(request.referrer or ""))[:500] or None,
                 pagina=_achatar(d.get("pagina"), 500) or None,
                 user_agent=(request.headers.get("User-Agent") or "")[:300], ip=(request.remote_addr or "")[:64])
    try:
        db.session.add(lead_)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Falha ao gravar lead (tenant %s)", tenant.codigo)
        return _responder(False, "Não foi possível enviar agora. Tente novamente em instantes.", 500, json_=quer_json)
    # 6) Notificações em segundo plano (o lead já está salvo)
    try:
        notificar_lead_async(lead_.id)
    except Exception:
        current_app.logger.exception("Falha ao agendar notificação do lead %s", lead_.id)
    extra = {"redirect": url_for("site.obrigado")}
    if tenant.whatsapp:
        extra["whatsapp_url"] = wa_link(tenant.whatsapp, f"Olá! Sou {nome} e acabei de enviar meu contato pelo site.")
    return _responder(True, "Recebemos seu contato! Em breve retornaremos.", json_=quer_json, proximo=proximo, **extra)


# ═══════════════════════════════════════════════════════════════════════════
# BLUEPRINTS.PAINEL — Painel do cliente (/painel, no PRÓPRIO domínio dele). Só conteúdo: leads, blog, imagens e dados básicos.
# ═══════════════════════════════════════════════════════════════════════════
bp_painel = Blueprint("painel", __name__, url_prefix="/painel")
ABERTAS_painel = {"painel.login", "painel.esqueci", "painel.redefinir", "painel.acesso"}


def _serializer(sal):
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=sal)


@bp_painel.before_request
def _antes_painel():
    if g.get("tenant") is None:
        abort(404)
    exigir_csrf()
    if request.endpoint in ABERTAS_painel:
        return None
    p = session.get("p")
    t = db.session.get(Tenant, g.tenant.id)          # sempre o tenant do Host, nunca um id vindo do cliente
    if not t or not t.ativo or not p or p.get("tid") != t.id or p.get("v") != t.sessao_versao:
        session.pop("p", None)
        prox = request.full_path.rstrip("?") if request.method == "GET" else None
        return redirect(url_for("painel.login", next=prox))
    g.t = t
    return None


@bp_painel.get("/")
def inicio():
    return redirect(url_for("painel.leads"))


@bp_painel.route("/login", methods=["GET", "POST"], endpoint="login")
def login_painel():
    cfg = current_app.config
    if request.method == "POST":
        chave = f"login:{g.tenant.id}:{request.remote_addr}"
        if excedido(chave, cfg["LOGIN_RATE_MAX"], cfg["LOGIN_RATE_JANELA"]):
            flash("Muitas tentativas de login. Aguarde alguns minutos.", "error")
            return render_template("painel/login.html", next=""), 429
        t = db.session.get(Tenant, g.tenant.id)
        if t and t.ativo and check_password_hash(t.senha_hash, request.form.get("senha", "")):
            session.clear()
            session["p"] = {"tid": t.id, "v": t.sessao_versao}
            session.permanent = True
            return redirect(safe_next(request.form.get("next")) or url_for("painel.leads"))
        registrar_ratelimit(chave)
        flash("Senha incorreta.", "error")
    return render_template("painel/login.html", next=request.args.get("next", ""))


@bp_painel.post("/sair", endpoint="sair")
def sair_painel():
    session.clear()
    return redirect(url_for("painel.login"))


@bp_painel.route("/esqueci", methods=["GET", "POST"])
def esqueci():
    cfg = current_app.config
    disponivel = smtp_configurado()
    if request.method == "POST":
        if not permitir(f"esqueci:{g.tenant.id}:{request.remote_addr}", 3, 3600):
            flash("Muitos pedidos. Tente novamente mais tarde.", "error")
            return render_template("painel/esqueci.html", disponivel=disponivel), 429
        t = db.session.get(Tenant, g.tenant.id)
        if disponivel and t and t.email_aviso:
            token = _serializer("reset-senha").dumps({"tid": t.id, "v": t.sessao_versao})
            link = url_for("painel.redefinir", t=token, _external=True)
            try:
                enviar_email(cfg, t.email_aviso, f"Redefinir senha do painel — {t.nome}",
                                    f"Para criar uma nova senha do painel, acesse (válido por 1 hora):\n{link}\n\n"
                                    "Se você não pediu isso, ignore este e-mail.")
            except Exception as e:
                current_app.logger.warning("Falha ao enviar e-mail de redefinição: %s", type(e).__name__)
        flash("Se houver um e-mail cadastrado para este site, enviamos o link de redefinição.", "success")
        return redirect(url_for("painel.login"))
    return render_template("painel/esqueci.html", disponivel=disponivel)


@bp_painel.route("/redefinir", methods=["GET", "POST"])
def redefinir():
    token = request.args.get("t", "")
    try:
        dados = _serializer("reset-senha").loads(token, max_age=3600)
    except BadData:
        flash("Link inválido ou expirado. Peça um novo.", "error")
        return redirect(url_for("painel.esqueci"))
    t = db.session.get(Tenant, g.tenant.id)
    if not t or dados.get("tid") != t.id or dados.get("v") != t.sessao_versao:    # uso único
        flash("Link inválido ou já utilizado. Peça um novo.", "error")
        return redirect(url_for("painel.esqueci"))
    if request.method == "POST":
        nova, conf = request.form.get("nova", ""), request.form.get("confirmar", "")
        try:
            if nova != conf:
                raise ErroNegocio("A confirmação não é igual à nova senha.")
            resetar_senha_services(t, nova)
            flash("Senha alterada. Entre com a nova senha.", "success")
            return redirect(url_for("painel.login"))
        except ErroNegocio as e:
            flash(str(e), "error")
    return render_template("painel/redefinir.html", token=token)


@bp_painel.get("/acesso")
def acesso():
    """Entrada por link assinado e temporário gerado pelo super-admin (impersonação)."""
    try:
        dados = _serializer("impersonar").loads(request.args.get("t", ""), max_age=120)
    except BadData:
        abort(404)
    t = db.session.get(Tenant, g.tenant.id)
    if not t or not t.ativo or dados.get("tid") != t.id:
        abort(404)
    session.clear()
    session["p"] = {"tid": t.id, "v": t.sessao_versao, "imp": True}
    session.permanent = False
    return redirect(url_for("painel.leads"))


# ───── leads ─────
def _filtros_painel():
    return {k: request.args.get(k, "") for k in ("q", "de", "ate", "status")}


@bp_painel.get("/leads", endpoint="leads")
def leads_painel():
    f = _filtros_painel()
    pag = paginar(consulta(g.t.id, **f), request.args.get("pagina", 1, type=int))
    novos = db.session.scalar(select(func.count(Lead.id)).where(Lead.tenant_id == g.t.id, Lead.atendido.is_(False)))
    return render_template("painel/leads.html", aba="leads", pag=pag, f=f, novos=novos or 0)


@bp_painel.get("/leads/exportar.csv", endpoint="leads_csv")
def leads_csv_painel():
    nome = f"leads-{g.t.codigo}-{para_local(agora_utc()).strftime('%Y%m%d')}.csv"
    return Response(gerar_csv(g.t.id, **_filtros_painel()), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{nome}"'})


@bp_painel.post("/leads/<int:lid>/atendido")
def lead_atendido(lid):
    l = db.session.scalars(select(Lead).where(Lead.id == lid, Lead.tenant_id == g.t.id)).first()
    if not l:
        abort(404)
    l.atendido = not l.atendido
    db.session.commit()
    return redirect(safe_next(request.form.get("voltar")) or url_for("painel.leads"))


# ───── blog ─────
def _post_do_tenant(pid):
    p = db.session.scalars(select(Post).where(Post.id == pid, Post.tenant_id == g.t.id)).first()
    if not p:
        abort(404)
    return p


def _slug_unico(base, ignorar_id=None):
    slug, n = base, 1
    while True:
        q = select(Post.id).where(Post.tenant_id == g.t.id, Post.slug == slug)
        if ignorar_id:
            q = q.where(Post.id != ignorar_id)
        if not db.session.scalar(q):
            return slug
        n += 1
        slug = f"{base[:110]}-{n}"


@bp_painel.get("/blog", endpoint="blog_lista")
def blog_lista_painel():
    posts = db.session.scalars(select(Post).where(Post.tenant_id == g.t.id).order_by(Post.criado_em.desc())).all()
    return render_template("painel/blog_lista.html", aba="blog", posts=posts)


def _dados_post(p=None):
    if p is None:
        return {"titulo": "", "slug": "", "resumo": "", "formato": "markdown", "corpo": "", "meta_description": "",
                "status": "rascunho", "publicado_em": "", "capa": None}
    pub = para_local(p.publicado_em).strftime("%Y-%m-%dT%H:%M") if p.publicado_em else ""
    return {"titulo": p.titulo, "slug": p.slug, "resumo": p.resumo or "", "formato": p.formato, "corpo": p.corpo,
            "meta_description": p.meta_description or "", "status": p.status, "publicado_em": pub, "capa": p.capa}


@bp_painel.route("/blog/novo", methods=["GET", "POST"], endpoint="blog_novo")
@bp_painel.route("/blog/<int:pid>/editar", methods=["GET", "POST"], endpoint="blog_editar")
def blog_form(pid=None):
    post = _post_do_tenant(pid) if pid else None
    dados = _dados_post(post)
    if request.method == "POST":
        f = request.form
        dados.update({k: f.get(k, "") for k in ("titulo", "slug", "resumo", "formato", "corpo", "meta_description",
                                                "status", "publicado_em")})
        try:
            titulo = sanitize_input(dados["titulo"])[:200]
            if not titulo:
                raise ErroNegocio("Informe o título do post.")
            formato = "markdown" if dados["formato"] == "markdown" else "html"
            corpo = dados["corpo"][:200_000]
            status = "publicado" if dados["status"] == "publicado" else "rascunho"
            if post is None:
                post = Post(tenant_id=g.t.id, titulo=titulo, slug="x", corpo="", corpo_html="")
                db.session.add(post)
            post.titulo = titulo
            post.slug = _slug_unico(gerar_slug(dados["slug"] or titulo, 110), post.id)
            post.resumo = sanitize_input(dados["resumo"])[:500] or None
            post.formato, post.corpo = formato, corpo
            post.corpo_html = markdown_html(corpo, g.t.codigo) if formato == "markdown" else html_seguro(corpo, g.t.codigo)
            post.meta_description = sanitize_input(dados["meta_description"])[:300] or None
            post.status = status
            dt = parse_datetime_local(dados["publicado_em"])
            if dt:
                post.publicado_em = local_para_utc(dt)
            elif status == "publicado" and not post.publicado_em:
                post.publicado_em = agora_utc()
            if f.get("remover_capa"):
                post.capa = None
            arq = request.files.get("capa")
            if arq and arq.filename:
                post.capa = salvar_galeria(g.t.codigo, arq, 1600)
            db.session.commit()
            flash("Post salvo." if status == "rascunho" else "Post publicado.", "success")
            return redirect(url_for("painel.blog_editar", pid=post.id))
        except (ErroNegocio, UploadError) as e:
            db.session.rollback()
            flash(str(e), "error")
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Erro ao salvar post")
            flash("Não foi possível salvar o post. Tente novamente.", "error")
        post = _post_do_tenant(pid) if pid else None
        dados["capa"] = post.capa if post else None
    return render_template("painel/blog_form.html", aba="blog", post=post, d=dados)


@bp_painel.post("/blog/<int:pid>/excluir")
def blog_excluir(pid):
    db.session.delete(_post_do_tenant(pid))
    db.session.commit()
    flash("Post excluído.", "success")
    return redirect(url_for("painel.blog_lista"))


@bp_painel.post("/blog/upload")
def blog_upload():
    try:
        nome = salvar_galeria(g.t.codigo, request.files.get("arquivo"))
    except UploadError as e:
        return jsonify(ok=False, erro=str(e)), 400
    return jsonify(ok=True, nome=nome, url=url_for("site.media", codigo=g.t.codigo, arquivo=nome))


@bp_painel.post("/blog/previa")
def blog_previa():
    corpo = request.form.get("corpo", "")[:200_000]
    html = markdown_html(corpo, g.t.codigo) if request.form.get("formato") == "markdown" else html_seguro(corpo, g.t.codigo)
    return jsonify(html=html)


# ───── imagens (tela gerada a partir do site.py) ─────
def _slots_do_tenant():
    site = obter(g.t.template)
    return site.slots if site else {}


@bp_painel.get("/imagens")
def imagens():
    idx = indice(g.t.codigo)
    cards = []
    for chave, cfg in _slots_do_tenant().items():
        r = idx.get(chave)
        cards.append({"chave": chave, **cfg, "enviada": bool(r), "mtime": r[1] if r else None})
    galeria = []
    for i in listar_galeria(g.t.codigo):
        i["url"] = url_for("site.media", codigo=g.t.codigo, arquivo=i["nome"], v=i["mtime"])
        i["url_abs"] = url_for("site.media", codigo=g.t.codigo, arquivo=i["nome"], v=i["mtime"], _external=True)
        i["thumb"] = i["url"] if i["nome"].endswith(".svg") else url_for(
            "site.media", codigo=g.t.codigo, arquivo=i["nome"], v=i["mtime"], w=400)
        galeria.append(i)
    return render_template("painel/imagens.html", aba="imagens", cards=cards, galeria=galeria,
                           volume=volume_info(), uso=uso_disco(g.t.codigo))


@bp_painel.post("/imagens/slot/<slot>")
def imagem_slot(slot):
    cfg = _slots_do_tenant().get(slot)
    if not cfg:
        abort(404)
    try:
        salvar_slot(g.t.codigo, slot, cfg, request.files.get("arquivo"))
        flash(f"“{cfg['nome']}” atualizada.", "success")
    except UploadError as e:
        flash(str(e), "error")
    return redirect(url_for("painel.imagens") + f"#slot-{slot}")


@bp_painel.post("/imagens/slot/<slot>/remover")
def imagem_slot_remover(slot):
    cfg = _slots_do_tenant().get(slot)
    if not cfg:
        abort(404)
    remover_slot(g.t.codigo, slot)
    flash(f"“{cfg['nome']}” removida. O site volta a usar a imagem padrão.", "success")
    return redirect(url_for("painel.imagens") + f"#slot-{slot}")


@bp_painel.post("/imagens/galeria")
def galeria_enviar():
    arquivos = [a for a in request.files.getlist("arquivos") if a and a.filename]
    if not arquivos:
        flash("Selecione ao menos uma imagem.", "error")
    ok = 0
    for a in arquivos[:20]:
        try:
            salvar_galeria(g.t.codigo, a)
            ok += 1
        except UploadError as e:
            flash(f"{a.filename[:60]}: {e}", "error")
    if ok:
        flash(f"{ok} imagem(ns) enviada(s).", "success")
    return redirect(url_for("painel.imagens") + "#galeria")


@bp_painel.post("/imagens/galeria/<nome>/excluir")
def galeria_excluir(nome):
    if not nome.startswith(PREFIXO_GALERIA) or not remover(g.t.codigo, nome):
        abort(404)
    flash("Imagem excluída. Se ela estava em algum post, o link deixará de funcionar.", "success")
    return redirect(url_for("painel.imagens") + "#galeria")


# ───── dados básicos ─────
@bp_painel.route("/dados", methods=["GET", "POST"])
def dados():
    if request.method == "POST":
        wa = normalizar_whatsapp(request.form.get("whatsapp", ""))
        email = sanitize_input(request.form.get("email_aviso", "")).lower()
        if wa is None:
            flash("WhatsApp inválido. Informe DDD + número (ex.: 11 99999-9999).", "error")
        elif email and not eh_email(email):
            flash("E-mail inválido.", "error")
        else:
            g.t.whatsapp, g.t.email_aviso = wa or None, email or None
            db.session.commit()
            limpar_cache_tenancy()
            flash("Dados salvos.", "success")
            return redirect(url_for("painel.dados"))
    return render_template("painel/dados.html", aba="dados")


@bp_painel.post("/dados/senha")
def dados_senha():
    try:
        trocar_senha_propria(g.t, request.form.get("atual"), request.form.get("nova"),
                                      request.form.get("confirmar"))
        session["p"] = {"tid": g.t.id, "v": g.t.sessao_versao}      # mantém ESTA sessão; derruba as outras
        flash("Senha alterada com sucesso.", "success")
    except ErroNegocio as e:
        flash(str(e), "error")
    return redirect(url_for("painel.dados"))


# ═══════════════════════════════════════════════════════════════════════════
# BLUEPRINTS.SUPERADMIN — Super-admin (só o dono): /_admin no ADMIN_HOST.
# ═══════════════════════════════════════════════════════════════════════════
bp_superadmin = Blueprint("superadmin", __name__, url_prefix="/_admin")
ABERTAS_superadmin = {"superadmin.login"}


@bp_superadmin.before_request
def _antes_superadmin():
    if not g.get("admin_host"):
        abort(404)                                  # /_admin só existe no ADMIN_HOST
    exigir_csrf()
    if request.endpoint in ABERTAS_superadmin:
        return None
    if not session.get("sa"):
        return redirect(url_for("superadmin.login"))
    return None


@bp_superadmin.route("/login", methods=["GET", "POST"], endpoint="login")
def login_superadmin():
    cfg = current_app.config
    if not cfg["ADMIN_USER"] or not cfg["ADMIN_PASS_HASH"]:
        return render_template("erro.html", codigo=503,
                               mensagem="Defina ADMIN_USER e ADMIN_PASS_HASH no .env (veja o README)."), 503
    if request.method == "POST":
        chave = f"admin:{request.remote_addr}"
        if excedido(chave, 5, 900):
            flash("Muitas tentativas. Aguarde 15 minutos.", "error")
            return render_template("superadmin/login.html"), 429
        usuario_ok = hmac.compare_digest(request.form.get("usuario", ""), cfg["ADMIN_USER"])
        senha_ok = check_password_hash(cfg["ADMIN_PASS_HASH"], request.form.get("senha", ""))
        if usuario_ok and senha_ok:
            session.clear()
            session["sa"] = {"u": cfg["ADMIN_USER"]}
            session.permanent = True
            return redirect(url_for("superadmin.lista"))
        registrar_ratelimit(chave)
        flash("Usuário ou senha incorretos.", "error")
    return render_template("superadmin/login.html")


@bp_superadmin.post("/sair", endpoint="sair")
def sair_superadmin():
    session.clear()
    return redirect(url_for("superadmin.login"))


def _tenant_ou_404(tid):
    t = db.session.get(Tenant, tid)
    if not t:
        abort(404)
    return t


def _filtros_superadmin():
    return {k: request.args.get(k, "") for k in ("q", "de", "ate", "status")}


@bp_superadmin.get("/")
def lista():
    tenants = db.session.scalars(select(Tenant).order_by(Tenant.criado_em.desc())).all()
    contagem = dict(db.session.execute(select(Lead.tenant_id, func.count(Lead.id)).group_by(Lead.tenant_id)).all())
    linhas = [{"t": t, "leads": contagem.get(t.id, 0), "disco": fmt_bytes(uso_disco(t.codigo)),
               "site_ok": obter(t.template) is not None} for t in tenants]
    return render_template("superadmin/tenants.html", linhas=linhas, volume=volume_info(),
                           sites_lista=listar())


def _form_tenant():
    f = request.form
    return dict(nome=f.get("nome", ""), dominios=f.get("dominios", ""), template=f.get("template", ""),
                email_aviso=f.get("email_aviso", ""), whatsapp=f.get("whatsapp", ""), webhook_url=f.get("webhook_url", ""))


@bp_superadmin.route("/tenants/novo", methods=["GET", "POST"])
def novo():
    dados = {"codigo": "", "nome": "", "dominios": "", "template": "", "email_aviso": "", "whatsapp": "",
             "webhook_url": "", "senha": ""}
    if request.method == "POST":
        dados.update(_form_tenant(), codigo=request.form.get("codigo", ""), senha=request.form.get("senha", ""))
        try:
            t, senha = criar_tenant(dados["codigo"], dados["nome"], dados["dominios"], dados["template"],
                                             senha=dados["senha"] or None, email_aviso=dados["email_aviso"],
                                             whatsapp=dados["whatsapp"], webhook_url=dados["webhook_url"])
            flash(f"Cliente {t.codigo} criado. Senha inicial do painel (mostrada só agora): {senha}", "senha")
            return redirect(url_for("superadmin.editar", tid=t.id))
        except ErroNegocio as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("superadmin/tenant_form.html", t=None, d=dados, sites_lista=listar())


@bp_superadmin.route("/tenants/<int:tid>", methods=["GET", "POST"])
def editar(tid):
    t = _tenant_ou_404(tid)
    dados = {"codigo": t.codigo, "nome": t.nome, "dominios": "\n".join(t.dominios_lista), "template": t.template,
             "email_aviso": t.email_aviso or "", "whatsapp": t.whatsapp or "", "webhook_url": t.webhook_url or ""}
    if request.method == "POST":
        dados.update(_form_tenant())
        try:
            atualizar_tenant(t, ativo=bool(request.form.get("ativo")), **_form_tenant())
            flash("Cliente atualizado.", "success")
            return redirect(url_for("superadmin.editar", tid=t.id))
        except ErroNegocio as e:
            db.session.rollback()
            flash(str(e), "error")
            t = _tenant_ou_404(tid)
    total = db.session.scalar(select(func.count(Lead.id)).where(Lead.tenant_id == t.id)) or 0
    return render_template("superadmin/tenant_form.html", t=t, d=dados, sites_lista=listar(), total_leads=total,
                           disco=fmt_bytes(uso_disco(t.codigo)))


@bp_superadmin.post("/tenants/<int:tid>/ativo")
def alternar_ativo(tid):
    t = _tenant_ou_404(tid)
    t.ativo = not t.ativo
    db.session.commit()
    limpar_cache_tenancy()
    flash(f"{t.codigo} {'ativado' if t.ativo else 'desativado'}.", "success")
    return redirect(safe_next(request.form.get("voltar")) or url_for("superadmin.lista"))


@bp_superadmin.post("/tenants/<int:tid>/senha", endpoint="resetar_senha")
def resetar_senha_superadmin(tid):
    t = _tenant_ou_404(tid)
    senha = resetar_senha_services(t)
    flash(f"Nova senha do painel de {t.codigo} (mostrada só agora): {senha}", "senha")
    return redirect(url_for("superadmin.editar", tid=t.id))


@bp_superadmin.post("/tenants/<int:tid>/excluir")
def excluir(tid):
    t = _tenant_ou_404(tid)
    if request.form.get("confirmar", "").strip() != t.codigo:
        flash("Para excluir, digite exatamente o código do cliente.", "error")
        return redirect(url_for("superadmin.editar", tid=t.id))
    codigo = t.codigo
    excluir_tenant(t)
    flash(f"Cliente {codigo} excluído (banco e imagens).", "success")
    return redirect(url_for("superadmin.lista"))


@bp_superadmin.get("/tenants/<int:tid>/leads", endpoint="leads")
def leads_superadmin(tid):
    t = _tenant_ou_404(tid)
    f = _filtros_superadmin()
    pag = paginar(consulta(t.id, **f), request.args.get("pagina", 1, type=int))
    return render_template("superadmin/leads.html", t=t, pag=pag, f=f)


@bp_superadmin.get("/tenants/<int:tid>/leads.csv", endpoint="leads_csv")
def leads_csv_superadmin(tid):
    t = _tenant_ou_404(tid)
    nome = f"leads-{t.codigo}-{para_local(agora_utc()).strftime('%Y%m%d')}.csv"
    return Response(gerar_csv(t.id, **_filtros_superadmin()), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{nome}"'})


@bp_superadmin.post("/tenants/<int:tid>/entrar")
def entrar(tid):
    """Impersonação: link assinado (2 min) para o painel do cliente, no domínio dele."""
    t = _tenant_ou_404(tid)
    token = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="impersonar").dumps({"tid": t.id})
    porta = request.host.partition(":")[2]
    sufixo = f":{porta}" if porta else ""
    if t.dominios_lista:
        url = f"{request.scheme}://{t.dominios_lista[0]}{sufixo}/painel/acesso?t={quote(token)}"
    elif current_app.config["ALLOW_TENANT_OVERRIDE"]:
        url = f"{request.scheme}://localhost{sufixo}/painel/acesso?t={quote(token)}&tenant={t.codigo}"
    else:
        flash("O cliente não tem domínio cadastrado.", "error")
        return redirect(url_for("superadmin.editar", tid=t.id))
    return redirect(url)


@bp_superadmin.post("/cache/limpar", endpoint="limpar_cache")
def limpar_cache_superadmin():
    limpar_cache_tenancy()
    invalidar_indice()
    descobrir(current_app.config["SITES_DIR"])
    flash("Cache limpo neste worker (domínios, índice de imagens e lista de sites). Os demais workers expiram em até "
          f"{current_app.config['TENANT_CACHE_TTL']}s.", "success")
    return redirect(safe_next(request.form.get("voltar")) or url_for("superadmin.lista"))


# ═══════════════════════════════════════════════════════════════════════════
# APP FACTORY — App factory da plataforma multi-tenant de landing pages.
# ═══════════════════════════════════════════════════════════════════════════
def create_app(config=None):
    app = Flask(__name__, template_folder=str(BASE_DIR / "templates"), static_folder=str(BASE_DIR / "static"))
    app.config.from_object(Config)
    app.config["SECRET_KEY"] = _secret_key(_DEBUG)           # falha aqui se SECRET_KEY faltar em produção
    if config:
        app.config.update(config)
    logging.basicConfig(level=logging.DEBUG if app.config["DEBUG"] else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    hops = app.config["PROXY_HOPS"]
    if hops > 0:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)

    # templates/ (painel, blog padrão) + sites/<lp_xxx>/... como "sites/lp_xxx/arquivo.html"
    app.jinja_loader = ChoiceLoader([FileSystemLoader(str(BASE_DIR / "templates")),
                                     PrefixLoader({"sites": FileSystemLoader(app.config["SITES_DIR"])})])

    db.init_app(app)
    registrar_helpers(app)
    descobrir(app.config["SITES_DIR"])

    for bp in (bp_site, bp_api, bp_painel, bp_superadmin):
        app.register_blueprint(bp)

    app.before_request(resolver_contexto)
    app.after_request(aplicar_cookie_tenant)

    @app.after_request
    def _seguranca(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.path.startswith(("/painel", "/_admin")):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    MENS = {400: "Requisição inválida ou sessão expirada. Volte, recarregue a página e tente de novo.",
            404: "Página não encontrada.", 405: "Ação não permitida.", 429: "Muitas tentativas. Aguarde alguns minutos.",
            500: "Erro interno. Tente novamente em instantes."}

    def responder(e):
        codigo = getattr(e, "code", 500) or 500
        msg = MENS.get(codigo) or "Erro"
        if codigo == 413:
            msg = f"Os arquivos enviados são grandes demais (limite de {app.config['MAX_CONTENT_LENGTH'] // 1048576} MB)."
        if request.path in ("/lead", "/api/lead") or request.accept_mimetypes.best == "application/json":
            return jsonify(ok=False, erro=msg), codigo
        if codigo == 500:
            app.logger.exception("Erro 500 em %s", request.path)
        t = g.get("tenant")
        if codigo == 404 and t and arquivo_existe(t.template, "404.html"):
            try:
                return render_template(f"sites/{t.template}/404.html"), 404
            except Exception:
                pass
        return render_template("erro.html", codigo=codigo, mensagem=msg), codigo

    for c in (400, 404, 405, 413, 429, 500):
        app.register_error_handler(c, responder)
    return app


# ═══════════════════════════════════════════════════════════════════════════
# CLI — Comandos: init-db | criar-tenant | listar-tenants | listar-sites | resetar-senha | gerar-hash
# ═══════════════════════════════════════════════════════════════════════════
#!/usr/bin/env python


def main():
    ap = argparse.ArgumentParser(description="Administração da plataforma de LPs")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="servidor de desenvolvimento (padrão)")
    sub.add_parser("init-db", help="cria/atualiza o banco (idempotente)")
    c = sub.add_parser("criar-tenant", help="cadastra um cliente")
    c.add_argument("--codigo", required=True, help="ex.: lpjulia (começa com lp)")
    c.add_argument("--dominio", required=True, action="append", help="repita para vários domínios")
    c.add_argument("--template", required=True, help="pasta em sites/, ex.: lp_julia")
    c.add_argument("--nome", help="nome do cliente (padrão: o código)")
    c.add_argument("--senha", help="senha do painel (padrão: gerada)")
    c.add_argument("--email", help="e-mail que recebe os leads")
    c.add_argument("--whatsapp", help="ex.: 11999999999")
    sub.add_parser("listar-tenants", help="lista os clientes")
    sub.add_parser("listar-sites", help="lista os templates encontrados em sites/")
    r = sub.add_parser("resetar-senha", help="define/gera nova senha do painel de um cliente")
    r.add_argument("--codigo", required=True)
    r.add_argument("--senha", help="nova senha (padrão: gerada)")
    sub.add_parser("gerar-hash", help="gera o ADMIN_PASS_HASH do super-admin")
    args = ap.parse_args()
    if args.cmd in (None, "run"):
        _app = create_app()
        iniciar_banco(_app)
        _app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")), debug=_app.config["DEBUG"])
        return

    if args.cmd == "gerar-hash":
        s1 = getpass.getpass("Senha do super-admin: ")
        if len(s1) < 10:
            sys.exit("Use pelo menos 10 caracteres.")
        if s1 != getpass.getpass("Repita a senha: "):
            sys.exit("As senhas não conferem.")
        print("\nCole no .env (com ASPAS SIMPLES, por causa dos '$'; no docker-compose.yml/.env use '$$' se precisar):\n")
        print(f"ADMIN_PASS_HASH='{generate_password_hash(s1)}'")
        return


    app = create_app()
    if args.cmd == "init-db":
        print(f"Banco pronto (schema v{iniciar_banco(app)}).")
        return
    iniciar_banco(app)
    with app.app_context():
        if args.cmd == "listar-sites":
            for tpl, nome in listar():
                print(f"{tpl:24} {nome}  (slots: {', '.join(obter(tpl).slots) or '-'})")
        elif args.cmd == "criar-tenant":
            try:
                t, senha = criar_tenant(args.codigo, args.nome or args.codigo, args.dominio, args.template,
                                                 senha=args.senha, email_aviso=args.email, whatsapp=args.whatsapp)
            except ErroNegocio as e:
                sys.exit(f"Erro: {e}")
            print(f"Cliente criado: {t.codigo}  domínios: {', '.join(t.dominios_lista)}  template: {t.template}")
            print(f"Painel: https://{t.dominios_lista[0]}/painel\nSenha inicial: {senha}")
        elif args.cmd == "listar-tenants":
            for t in db.session.scalars(select(Tenant).order_by(Tenant.id)):
                n = db.session.scalar(select(func.count(Lead.id)).where(Lead.tenant_id == t.id))
                print(f"{t.id:3} {t.codigo:20} {'ativo  ' if t.ativo else 'INATIVO'} {t.template:18} leads={n:<5} "
                      f"{', '.join(t.dominios_lista)}")
        elif args.cmd == "resetar-senha":
            t = db.session.scalars(select(Tenant).where(Tenant.codigo == args.codigo)).first()
            if not t:
                sys.exit(f"Cliente {args.codigo} não encontrado.")
            try:
                print(f"Nova senha de {t.codigo}: {resetar_senha_services(t, args.senha)}")
            except ErroNegocio as e:
                sys.exit(f"Erro: {e}")


if __name__ == "__main__":
    main()
else:
    app = create_app()          # gunicorn app:app
