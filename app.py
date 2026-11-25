"""Leanttro CRM — Flask + SQLAlchemy, um arquivo só.

Contas e nichos: um app só. Quem não tem conta usa o CRM de agência; quem tem conta (criada em /admin/contas)
cai no painel do nicho da conta (nichos/<nicho>/nicho.json), com os dados isolados por conta.

Dev:   python app.py                (HOST/PORT/DEBUG por env)
Check: python app.py check-nicho    (valida todos os nichos/*/nicho.json)
Prod:  gunicorn app:app             (docker-entrypoint.sh roda `python app.py init-db` antes)
CLI:   python app.py init-db
Banco: SQLite em DATA_DIR/db.sqlite (Docker: volume /data). DATABASE_URL só se quiser Postgres.
Dinheiro: sempre em CENTAVOS (inteiro). Percentuais: float (40.0 = 40%).
"""
import base64
import calendar
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache, wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from flask import Flask, abort, g, jsonify, redirect, render_template, request, send_from_directory, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, UniqueConstraint, and_, delete, event, func, inspect, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


# ═══════════════════════════════════════════════════════════════════════════
# CONFIG / ENV
# ═══════════════════════════════════════════════════════════════════════════
def _env(nome, padrao=""):
    return os.environ.get(nome, padrao).strip()


def _int(nome, padrao):
    try:
        return int(_env(nome) or padrao)
    except ValueError:
        return padrao


DEBUG = _env("DEBUG") == "1"
DATA_DIR = Path(_env("DATA_DIR") or (BASE_DIR / "data")).resolve()
TZ = ZoneInfo(_env("TZ_NAME") or "America/Sao_Paulo")
SCHEMA_VERSAO = 2
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
NICHOS_DIR = BASE_DIR / "nichos"                   # um nicho = uma pasta com nicho.json


def _segredo(nome):
    """Env tem prioridade. Sem env, gera e guarda em DATA_DIR/.<nome> (sobrevive a reinício do container)."""
    v = _env(nome)
    if v:
        return v
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    arq = DATA_DIR / f".{nome.lower()}"
    if not arq.exists():
        arq.write_text(secrets.token_urlsafe(48))
        os.chmod(arq, 0o600)
    return arq.read_text().strip()


def _db_url():
    url = _env("DATABASE_URL")
    if not url:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return "sqlite:///" + str(DATA_DIR / "db.sqlite")
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def agora():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hoje():
    return datetime.now(TZ).date()


db = SQLAlchemy()


@event.listens_for(Engine, "connect")
def _pragmas_sqlite(conexao, _registro):
    """SQLite: WAL, espera em vez de 'database is locked' e FKs ligadas (cascade funciona)."""
    if isinstance(conexao, sqlite3.Connection):
        cur = conexao.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


# ═══════════════════════════════════════════════════════════════════════════
# COFRE DE CHAVES (Groq / Serper de cada vendedor) — cifradas em repouso
# ═══════════════════════════════════════════════════════════════════════════
@lru_cache(maxsize=1)
def _fernet():
    chave = hashlib.sha256(_segredo("APP_KEY").encode()).digest()
    return Fernet(base64.urlsafe_b64encode(chave))


def cifrar(txt):
    return _fernet().encrypt(txt.encode()).decode() if txt else ""


def decifrar(tok):
    if not tok:
        return ""
    try:
        return _fernet().decrypt(tok.encode()).decode()
    except InvalidToken:
        return ""


def mascarar(tok):
    v = decifrar(tok)
    return ("•••• " + v[-4:]) if v else ""


# ═══════════════════════════════════════════════════════════════════════════
# MODELS
#   Acesso:     users, rate_hits, api_uso, config, schema_info
#   Prospecção: buscas, busca_resultados
#   Vendas:     servicos, leads, lead_historico, atividades, propostas, vendas, pagamentos, comissoes
#   Operação:   clientes, projetos, projeto_horas, materiais
#   Financeiro: custos
#   Gestão:     candidatos, treinamento_modulos, treinamento_progresso
# ═══════════════════════════════════════════════════════════════════════════
LEAD_STATUS = ("novo", "em_contato", "qualificado", "proposta_enviada", "negociacao", "fechado", "perdido")
PAPEIS = ("admin", "vendedor")


class User(db.Model):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("papel in ('admin','vendedor')", name="ck_users_papel"),
        CheckConstraint("status in ('ativo','bloqueado')", name="ck_users_status"),
    )
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(254), nullable=False)
    email_norm = db.Column(db.String(254), nullable=False, unique=True)
    senha_hash = db.Column(db.String(255), nullable=False)
    papel = db.Column(db.String(10), nullable=False, default="vendedor")
    status = db.Column(db.String(10), nullable=False, default="ativo", index=True)
    telefone = db.Column(db.String(30))
    # percentuais próprios (NULL = usa o global da tabela config)
    comissao_setup_pct = db.Column(db.Float)
    comissao_mensal_pct = db.Column(db.Float)
    # chaves de API do vendedor, cifradas (Fernet)
    groq_key_enc = db.Column(db.Text, nullable=False, default="")
    serper_key_enc = db.Column(db.Text, nullable=False, default="")
    trocar_senha = db.Column(db.Boolean, nullable=False, default=False)
    sessao_versao = db.Column(db.Integer, nullable=False, default=1)   # +1 derruba todas as sessões da pessoa
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)
    ultimo_login = db.Column(db.DateTime)
    conta_id = db.Column(db.Integer, db.ForeignKey("contas.id"), index=True)   # NULL = equipe da agência

    @property
    def is_admin(self):
        return self.papel == "admin"


class RateHit(db.Model):
    __tablename__ = "rate_hits"
    id = db.Column(db.Integer, primary_key=True)
    chave = db.Column(db.String(160), nullable=False, index=True)
    ts = db.Column(db.Float, nullable=False)


class ApiUso(db.Model):
    """Consumo de APIs por vendedor (alimenta limites e o custo variável de APIs)."""
    __tablename__ = "api_uso"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    servico = db.Column(db.String(10), nullable=False)                 # serper | groq
    qtd = db.Column(db.Integer, nullable=False, default=1)
    ts = db.Column(db.DateTime, nullable=False, default=agora)


class Config(db.Model):
    __tablename__ = "config"
    chave = db.Column(db.String(60), primary_key=True)
    valor = db.Column(db.Text, nullable=False, default="")


class SchemaInfo(db.Model):
    __tablename__ = "schema_info"
    id = db.Column(db.Integer, primary_key=True)
    versao = db.Column(db.Integer, nullable=False, default=1)


class Busca(db.Model):
    __tablename__ = "buscas"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    consulta = db.Column(db.String(200), nullable=False)
    cidade = db.Column(db.String(120))
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)


class BuscaResultado(db.Model):
    """Resultado bruto do Serper. É 'potencial', nunca contato validado."""
    __tablename__ = "busca_resultados"
    id = db.Column(db.Integer, primary_key=True)
    busca_id = db.Column(db.Integer, db.ForeignKey("buscas.id", ondelete="CASCADE"), nullable=False, index=True)
    titulo = db.Column(db.String(200), nullable=False)
    site = db.Column(db.String(300))
    telefone = db.Column(db.String(40))
    endereco = db.Column(db.String(300))
    raw_json = db.Column(db.Text)
    status = db.Column(db.String(12), nullable=False, default="novo")  # novo | importado | descartado
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id", ondelete="SET NULL"))


class Servico(db.Model):
    __tablename__ = "servicos"
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    descricao = db.Column(db.Text, nullable=False, default="")
    preco_setup_cent = db.Column(db.Integer, nullable=False, default=0)
    preco_mensal_cent = db.Column(db.Integer, nullable=False, default=0)
    ativo = db.Column(db.Boolean, nullable=False, default=True)


class Lead(db.Model):
    __tablename__ = "leads"
    __table_args__ = (CheckConstraint("status in (" + ",".join(f"'{s}'" for s in LEAD_STATUS) + ")", name="ck_leads_status"),)
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    empresa = db.Column(db.String(160), nullable=False)
    contato = db.Column(db.String(120))
    telefone = db.Column(db.String(40))
    whatsapp = db.Column(db.String(40))
    email = db.Column(db.String(254))
    site = db.Column(db.String(300))
    segmento = db.Column(db.String(80))
    cidade = db.Column(db.String(120))
    origem = db.Column(db.String(20), nullable=False, default="manual")  # serper | manual | indicacao
    servico_id = db.Column(db.Integer, db.ForeignKey("servicos.id"))
    valor_estimado_cent = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default="novo", index=True)
    proxima_atividade_em = db.Column(db.DateTime)
    motivo_perda = db.Column(db.String(200))
    dedupe_key = db.Column(db.String(300), unique=True)                 # domínio do site ou telefone (só dígitos)
    versao = db.Column(db.Integer, nullable=False, default=1)           # trava otimista
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)
    atualizado_em = db.Column(db.DateTime, nullable=False, default=agora, onupdate=agora)


class LeadHistorico(db.Model):
    __tablename__ = "lead_historico"
    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    tipo = db.Column(db.String(12), nullable=False, default="nota")     # nota | contato | status | proposta
    texto = db.Column(db.Text, nullable=False, default="")
    status_de = db.Column(db.String(20))
    status_para = db.Column(db.String(20))
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)


class Atividade(db.Model):
    __tablename__ = "atividades"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id", ondelete="CASCADE"))
    cliente_id = db.Column(db.Integer, db.ForeignKey("clientes.id", ondelete="CASCADE"))
    titulo = db.Column(db.String(200), nullable=False)
    vence_em = db.Column(db.DateTime)
    feita_em = db.Column(db.DateTime)
    registro_id = db.Column(db.Integer, db.ForeignKey("registros.id", ondelete="CASCADE"))   # nichos
    auto_chave = db.Column(db.String(80))                               # lembrete automático do manifesto


class Cliente(db.Model):
    """Nasce de um lead fechado (lead_id) ou de cadastro manual direto (origem='manual')."""
    __tablename__ = "clientes"
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id", ondelete="SET NULL"), unique=True)
    empresa = db.Column(db.String(160), nullable=False)
    contato = db.Column(db.String(120))
    documento = db.Column(db.String(20))                                # CNPJ/CPF
    telefone = db.Column(db.String(40))
    email = db.Column(db.String(254))
    site = db.Column(db.String(300))
    endereco = db.Column(db.String(300))
    origem = db.Column(db.String(10), nullable=False, default="manual")  # manual | lead
    observacoes = db.Column(db.Text, nullable=False, default="")
    dados = db.Column(db.JSON)                                          # nichos: campos_cliente do manifesto
    conta_id = db.Column(db.Integer, db.ForeignKey("contas.id"), index=True)   # nichos: a qual conta pertence
    portal_hash = db.Column(db.String(64), index=True)                  # sha256 do token do link do cliente final
    portal_token_enc = db.Column(db.Text)                               # token cifrado (para a loja copiar o link de novo)
    portal_aceite_em = db.Column(db.DateTime)                           # quando o cliente aceitou a política
    portal_aceite_versao = db.Column(db.Integer)
    portal_ultimo_acesso = db.Column(db.DateTime)
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)


class Proposta(db.Model):
    __tablename__ = "propostas"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id", ondelete="SET NULL"))
    cliente_id = db.Column(db.Integer, db.ForeignKey("clientes.id", ondelete="SET NULL"))
    servico_id = db.Column(db.Integer, db.ForeignKey("servicos.id"))
    valor_setup_cent = db.Column(db.Integer, nullable=False, default=0)
    valor_mensal_cent = db.Column(db.Integer, nullable=False, default=0)
    condicoes = db.Column(db.Text, nullable=False, default="")
    validade = db.Column(db.Date)
    status = db.Column(db.String(12), nullable=False, default="rascunho")  # rascunho | enviada | aceita | recusada
    pdf_path = db.Column(db.String(300))                                # relativo a DATA_DIR
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)


class Venda(db.Model):
    __tablename__ = "vendas"
    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id", ondelete="SET NULL"))
    cliente_id = db.Column(db.Integer, db.ForeignKey("clientes.id"), nullable=False, index=True)
    vendedor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    servico_id = db.Column(db.Integer, db.ForeignKey("servicos.id"))
    proposta_id = db.Column(db.Integer, db.ForeignKey("propostas.id", ondelete="SET NULL"))
    valor_setup_cent = db.Column(db.Integer, nullable=False, default=0)
    valor_mensal_cent = db.Column(db.Integer, nullable=False, default=0)
    # percentuais GRAVADOS no fechamento (mudar a regra global depois não altera vendas antigas)
    pct_setup = db.Column(db.Float, nullable=False, default=0)
    pct_mensal = db.Column(db.Float, nullable=False, default=0)
    mensal_ativa = db.Column(db.Boolean, nullable=False, default=False)  # False = cliente cancelou a hospedagem
    mensal_inicio = db.Column(db.Date)                                   # vencimento da 1ª mensalidade
    fechada_em = db.Column(db.Date, nullable=False, default=hoje)
    versao = db.Column(db.Integer, nullable=False, default=1)
    pagamentos = db.relationship("Pagamento", backref="venda", cascade="all, delete-orphan", passive_deletes=True)


class Pagamento(db.Model):
    __tablename__ = "pagamentos"
    __table_args__ = (UniqueConstraint("venda_id", "tipo", "competencia", name="uq_pagamento_competencia"),
                      CheckConstraint("tipo in ('setup','mensalidade')", name="ck_pagamento_tipo"))
    id = db.Column(db.Integer, primary_key=True)
    venda_id = db.Column(db.Integer, db.ForeignKey("vendas.id", ondelete="CASCADE"), nullable=False, index=True)
    tipo = db.Column(db.String(12), nullable=False)
    competencia = db.Column(db.String(7), nullable=False)               # 'setup' ou 'YYYY-MM'
    valor_cent = db.Column(db.Integer, nullable=False)                  # previsto
    valor_recebido_cent = db.Column(db.Integer, nullable=False, default=0)
    vence_em = db.Column(db.Date, nullable=False)
    pago_em = db.Column(db.Date)                                        # data do último recebimento
    comissao = db.relationship("Comissao", uselist=False, backref="pagamento",
                               cascade="all, delete-orphan", passive_deletes=True)

    @property
    def status(self):
        if self.valor_recebido_cent >= self.valor_cent:
            return "pago"
        if self.valor_recebido_cent > 0:
            return "parcial"
        return "atrasado" if self.vence_em < hoje() else "pendente"


class Comissao(db.Model):
    """Uma por pagamento. Libera proporcionalmente ao que foi RECEBIDO."""
    __tablename__ = "comissoes"
    id = db.Column(db.Integer, primary_key=True)
    pagamento_id = db.Column(db.Integer, db.ForeignKey("pagamentos.id", ondelete="CASCADE"), nullable=False, unique=True)
    venda_id = db.Column(db.Integer, db.ForeignKey("vendas.id", ondelete="CASCADE"), nullable=False, index=True)
    vendedor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    pct = db.Column(db.Float, nullable=False)
    valor_previsto_cent = db.Column(db.Integer, nullable=False)
    valor_liberado_cent = db.Column(db.Integer, nullable=False, default=0)
    valor_pago_cent = db.Column(db.Integer, nullable=False, default=0)
    pago_em = db.Column(db.Date)

    @property
    def a_pagar_cent(self):
        return max(self.valor_liberado_cent - self.valor_pago_cent, 0)

    @property
    def status(self):
        if self.valor_liberado_cent == 0:
            return "aguardando"          # cliente ainda não pagou
        if self.a_pagar_cent > 0:
            return "liberada"            # tem dinheiro a repassar ao vendedor
        if self.valor_pago_cent >= self.valor_previsto_cent:
            return "paga"
        return "parcial"                 # repassado o que foi liberado; falta o cliente pagar o resto


class Projeto(db.Model):
    __tablename__ = "projetos"
    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey("clientes.id", ondelete="CASCADE"), nullable=False, index=True)
    venda_id = db.Column(db.Integer, db.ForeignKey("vendas.id", ondelete="SET NULL"))
    nome = db.Column(db.String(160), nullable=False)
    status = db.Column(db.String(12), nullable=False, default="a_iniciar")  # a_iniciar | andamento | entregue
    prazo = db.Column(db.Date)
    custo_hora_cent = db.Column(db.Integer, nullable=False, default=0)
    observacoes = db.Column(db.Text, nullable=False, default="")


class ProjetoHoras(db.Model):
    __tablename__ = "projeto_horas"
    id = db.Column(db.Integer, primary_key=True)
    projeto_id = db.Column(db.Integer, db.ForeignKey("projetos.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    horas = db.Column(db.Float, nullable=False)
    data = db.Column(db.Date, nullable=False, default=hoje)
    descricao = db.Column(db.String(200))


class Material(db.Model):
    __tablename__ = "materiais"
    id = db.Column(db.Integer, primary_key=True)
    titulo = db.Column(db.String(160), nullable=False)
    categoria = db.Column(db.String(60), nullable=False, default="geral")
    arquivo = db.Column(db.String(300))                                 # relativo a DATA_DIR/uploads
    link = db.Column(db.String(500))


class Custo(db.Model):
    __tablename__ = "custos"
    __table_args__ = (CheckConstraint("tipo in ('fixo','variavel')", name="ck_custos_tipo"),
                      CheckConstraint("recorrencia in ('mensal','anual','unica')", name="ck_custos_rec"))
    id = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.String(10), nullable=False)
    categoria = db.Column(db.String(60), nullable=False)
    descricao = db.Column(db.String(200), nullable=False, default="")
    valor_cent = db.Column(db.Integer, nullable=False)
    recorrencia = db.Column(db.String(10), nullable=False, default="unica")
    data = db.Column(db.Date, nullable=False, default=hoje)
    projeto_id = db.Column(db.Integer, db.ForeignKey("projetos.id", ondelete="SET NULL"))


class Candidato(db.Model):
    __tablename__ = "candidatos"
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(254))
    telefone = db.Column(db.String(40))
    etapa = db.Column(db.String(20), nullable=False, default="novo")
    notas = db.Column(db.Text, nullable=False, default="")
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))  # preenchido na aprovação
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)


class TreinamentoModulo(db.Model):
    __tablename__ = "treinamento_modulos"
    id = db.Column(db.Integer, primary_key=True)
    titulo = db.Column(db.String(160), nullable=False)
    ordem = db.Column(db.Integer, nullable=False, default=0)
    conteudo = db.Column(db.Text, nullable=False, default="")


class TreinamentoProgresso(db.Model):
    __tablename__ = "treinamento_progresso"
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    modulo_id = db.Column(db.Integer, db.ForeignKey("treinamento_modulos.id", ondelete="CASCADE"), primary_key=True)
    concluido_em = db.Column(db.DateTime, nullable=False, default=agora)


# ═══════════════════════════════════════════════════════════════════════════
# NICHOS (CRMs focados): modelos genéricos. O que cada nicho tem de diferente
# mora em nichos/<nicho>/nicho.json; aqui só existe a estrutura que serve a todos.
#   Conta               = um cliente seu; escolhe o nicho; isola os dados
#   Registro            = "o que acontece com o cliente" (consulta, receita, encomenda, ordem de serviço…)
#   RegistroPagamento   = cobranças de um registro (sinal, restante, parcelas)
#   RegistroHistorico   = linha do tempo do registro (notas e mudanças de etapa)
# Regra: o que se filtra/ordena/soma é coluna (status, data_ref, valor_cent); o resto vai em `dados` (JSON).
# ═══════════════════════════════════════════════════════════════════════════
class Conta(db.Model):
    """Um cliente seu (ex.: "Ótica Visão"). O nicho da conta define o painel de quem entra; os dados ficam isolados por conta.
    Quem NÃO tem conta (você e seus vendedores) usa o CRM de agência."""
    __tablename__ = "contas"
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    nicho = db.Column(db.String(40), nullable=False)                     # pasta em nichos/
    ativo = db.Column(db.Boolean, nullable=False, default=True)          # False = suspensa: ninguém da conta entra
    taxas = db.Column(db.JSON)                                           # % da maquininha: {"debito": 1.9, "c1": 3.4, ..., "c12": 12.5}
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)


class Registro(db.Model):
    __tablename__ = "registros"
    id = db.Column(db.Integer, primary_key=True)
    conta_id = db.Column(db.Integer, db.ForeignKey("contas.id"), index=True)   # isolamento entre clientes
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey("clientes.id", ondelete="CASCADE"), nullable=False, index=True)
    tipo = db.Column(db.String(40), nullable=False, index=True)          # id em tipos_registro do manifesto
    titulo = db.Column(db.String(200), nullable=False, default="")       # derivado de dados[titulo]
    status = db.Column(db.String(30), nullable=False, index=True)        # uma das etapas do tipo
    data_ref = db.Column(db.Date, index=True)                            # derivada de dados[data_ref] (ou criação)
    valor_cent = db.Column(db.Integer, nullable=False, default=0)        # derivado de dados[valor]
    dados = db.Column(db.JSON, nullable=False, default=dict)
    versao = db.Column(db.Integer, nullable=False, default=1)            # trava otimista
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)
    atualizado_em = db.Column(db.DateTime, nullable=False, default=agora, onupdate=agora)
    pagamentos = db.relationship("RegistroPagamento", backref="registro", cascade="all, delete-orphan",
                                 passive_deletes=True, order_by="RegistroPagamento.vence_em")


class RegistroPagamento(db.Model):
    __tablename__ = "registro_pagamentos"
    id = db.Column(db.Integer, primary_key=True)
    registro_id = db.Column(db.Integer, db.ForeignKey("registros.id", ondelete="CASCADE"), nullable=False, index=True)
    descricao = db.Column(db.String(120), nullable=False, default="Pagamento")
    valor_cent = db.Column(db.Integer, nullable=False)                   # previsto
    valor_recebido_cent = db.Column(db.Integer, nullable=False, default=0)
    vence_em = db.Column(db.Date, nullable=False)
    pago_em = db.Column(db.Date)
    forma = db.Column(db.String(12))                                     # pix | dinheiro | debito | credito | boleto | outro
    parcelas = db.Column(db.Integer)                                     # só para crédito (1 = à vista)
    taxa_cent = db.Column(db.Integer, nullable=False, default=0)         # taxa da maquininha sobre o recebido

    @property
    def status(self):
        if self.valor_recebido_cent >= self.valor_cent:
            return "pago"
        if self.valor_recebido_cent > 0:
            return "parcial"
        return "atrasado" if self.vence_em < hoje() else "pendente"


class RegistroHistorico(db.Model):
    __tablename__ = "registro_historico"
    id = db.Column(db.Integer, primary_key=True)
    registro_id = db.Column(db.Integer, db.ForeignKey("registros.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    tipo = db.Column(db.String(12), nullable=False, default="nota")      # nota | status
    texto = db.Column(db.Text, nullable=False, default="")
    status_de = db.Column(db.String(30))
    status_para = db.Column(db.String(30))
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)


class CustoCliente(db.Model):
    """Gasto que você teve com UM cliente (material, laboratório, frete...). Serve para ver o lucro de verdade."""
    __tablename__ = "custos_cliente"
    id = db.Column(db.Integer, primary_key=True)
    conta_id = db.Column(db.Integer, db.ForeignKey("contas.id"), nullable=False, index=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    cliente_id = db.Column(db.Integer, db.ForeignKey("clientes.id", ondelete="CASCADE"), nullable=False, index=True)
    registro_id = db.Column(db.Integer, db.ForeignKey("registros.id", ondelete="SET NULL"), index=True)   # opcional
    categoria = db.Column(db.String(40), nullable=False, default="Outros")
    descricao = db.Column(db.String(160), nullable=False, default="")
    valor_cent = db.Column(db.Integer, nullable=False)
    data = db.Column(db.Date, nullable=False, default=hoje)
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)


class PortalMsg(db.Model):
    """Conversa entre a loja e o cliente final (pela área do cliente). Pode levar UM arquivo (ex.: foto da receita nova)."""
    __tablename__ = "portal_msgs"
    id = db.Column(db.Integer, primary_key=True)
    conta_id = db.Column(db.Integer, db.ForeignKey("contas.id"), nullable=False, index=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey("clientes.id", ondelete="CASCADE"), nullable=False, index=True)
    de = db.Column(db.String(8), nullable=False)                         # cliente | loja
    tipo = db.Column(db.String(12), nullable=False, default="mensagem")  # mensagem | pedido | receita | dados
    texto = db.Column(db.Text, nullable=False, default="")
    arquivo = db.Column(db.String(300))
    nome = db.Column(db.String(200))
    mime = db.Column(db.String(60))
    tamanho = db.Column(db.Integer)
    lida = db.Column(db.Boolean, nullable=False, default=False)          # lida pela loja (só importa para de='cliente')
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)


class Anexo(db.Model):
    """Arquivo (foto/PDF da receita etc.) preso a um registro. O arquivo fica em DATA_DIR/uploads/contas/<conta>/, não no banco."""
    __tablename__ = "anexos"
    id = db.Column(db.Integer, primary_key=True)
    conta_id = db.Column(db.Integer, db.ForeignKey("contas.id"), nullable=False, index=True)
    registro_id = db.Column(db.Integer, db.ForeignKey("registros.id", ondelete="CASCADE"), nullable=False, index=True)
    nome = db.Column(db.String(200), nullable=False)
    arquivo = db.Column(db.String(300), nullable=False)                  # relativo a DATA_DIR/uploads
    mime = db.Column(db.String(60), nullable=False)
    tamanho = db.Column(db.Integer, nullable=False, default=0)
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)


class Produto(db.Model):
    """Catálogo e precificação por conta. Custos e preço são informados por unidade ou por cento."""
    __tablename__ = "produtos"
    id = db.Column(db.Integer, primary_key=True)
    conta_id = db.Column(db.Integer, db.ForeignKey("contas.id"), nullable=False, index=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    nome = db.Column(db.String(160), nullable=False)
    unidade_venda = db.Column(db.String(10), nullable=False, default="unidade")  # unidade | cento
    custo_fixo_cent = db.Column(db.Integer, nullable=False, default=0)
    custo_variavel_cent = db.Column(db.Integer, nullable=False, default=0)
    preco_venda_cent = db.Column(db.Integer, nullable=False, default=0)
    lucro_pct = db.Column(db.Float, nullable=False, default=100)
    observacoes = db.Column(db.String(500), nullable=False, default="")
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)
    atualizado_em = db.Column(db.DateTime, nullable=False, default=agora, onupdate=agora)


# ═══════════════════════════════════════════════════════════════════════════
# MIGRAÇÃO SIMPLES (sem Alembic)
# ═══════════════════════════════════════════════════════════════════════════
def _add_col(tabela, coluna, ddl):
    """ALTER TABLE ADD COLUMN só se a coluna ainda não existe."""
    cols = {c["name"] for c in inspect(db.engine).get_columns(tabela)}
    if coluna not in cols:
        db.session.execute(text(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {ddl}"))
        db.session.commit()


# ═══════════════════════════════════════════════════════════════════════════
# REGRAS DE NEGÓCIO: percentuais, vendas, pagamentos, comissões
# ═══════════════════════════════════════════════════════════════════════════
CONFIG_PADRAO = {"comissao_setup_pct": "40", "comissao_mensal_pct": "20"}


def cfg_get(chave, padrao=""):
    c = db.session.get(Config, chave)
    return c.valor if c else CONFIG_PADRAO.get(chave, padrao)


def cfg_set(chave, valor):
    c = db.session.get(Config, chave)
    if c:
        c.valor = str(valor)
    else:
        db.session.add(Config(chave=chave, valor=str(valor)))


def pct_efetivo(vendedor, tipo):
    """Ordem: (venda, tratada na criação) > vendedor > global."""
    campo = "comissao_setup_pct" if tipo == "setup" else "comissao_mensal_pct"
    v = getattr(vendedor, campo)
    return float(v) if v is not None else float(cfg_get(campo, "0"))


def _add_meses(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _novo_pagamento(venda, tipo, competencia, valor_cent, vence):
    p = Pagamento(venda_id=venda.id, tipo=tipo, competencia=competencia, valor_cent=valor_cent, vence_em=vence)
    db.session.add(p)
    db.session.flush()
    pct = venda.pct_setup if tipo == "setup" else venda.pct_mensal
    db.session.add(Comissao(pagamento_id=p.id, venda_id=venda.id, vendedor_id=venda.vendedor_id,
                            pct=pct, valor_previsto_cent=round(valor_cent * pct / 100)))
    return p


def manter_mensalidades(venda, meses=12):
    """Mantém criadas as mensalidades dos próximos `meses`. Idempotente. Só enquanto mensal_ativa."""
    if not venda.mensal_ativa or venda.valor_mensal_cent <= 0 or not venda.mensal_inicio:
        return 0
    existentes = set(db.session.scalars(select(Pagamento.competencia).where(
        Pagamento.venda_id == venda.id, Pagamento.tipo == "mensalidade")))
    limite, k, criados = _add_meses(hoje(), meses), 0, 0
    while True:
        venc = _add_meses(venda.mensal_inicio, k)
        if venc > limite:
            break
        comp = venc.strftime("%Y-%m")
        if comp not in existentes:
            _novo_pagamento(venda, "mensalidade", comp, venda.valor_mensal_cent, venc)
            criados += 1
        k += 1
    return criados


def manter_mensalidades_todas():
    n = 0
    for v in db.session.scalars(select(Venda).where(Venda.mensal_ativa.is_(True))):
        n += manter_mensalidades(v)
    db.session.commit()
    return n


def criar_venda(cliente, vendedor, setup_cent=0, mensal_cent=0, servico_id=None, lead=None, proposta_id=None,
                fechada_em=None, setup_vence=None, mensal_inicio=None, pct_setup=None, pct_mensal=None):
    """Fechamento numa transação só: venda + pagamentos + comissões + projeto. O caller dá commit."""
    fechada_em = fechada_em or hoje()
    venda = Venda(
        lead_id=lead.id if lead else None, cliente_id=cliente.id, vendedor_id=vendedor.id,
        servico_id=servico_id, proposta_id=proposta_id,
        valor_setup_cent=setup_cent, valor_mensal_cent=mensal_cent,
        pct_setup=pct_setup if pct_setup is not None else pct_efetivo(vendedor, "setup"),
        pct_mensal=pct_mensal if pct_mensal is not None else pct_efetivo(vendedor, "mensal"),
        mensal_ativa=mensal_cent > 0, fechada_em=fechada_em,
        mensal_inicio=(mensal_inicio or _add_meses(fechada_em, 1)) if mensal_cent > 0 else None)
    db.session.add(venda)
    db.session.flush()
    if setup_cent > 0:
        _novo_pagamento(venda, "setup", "setup", setup_cent, setup_vence or fechada_em)
    manter_mensalidades(venda)
    srv = db.session.get(Servico, servico_id) if servico_id else None
    db.session.add(Projeto(cliente_id=cliente.id, venda_id=venda.id,
                           nome=f"{srv.nome if srv else 'Projeto'} — {cliente.empresa}"[:160]))
    if lead:
        antigo, lead.status = lead.status, "fechado"
        db.session.add(LeadHistorico(lead_id=lead.id, user_id=vendedor.id, tipo="status",
                                     texto="Venda fechada", status_de=antigo, status_para="fechado"))
    return venda


def receber_pagamento(pag, total_recebido_cent, data=None):
    """Define o TOTAL recebido do pagamento (aceita parcial) e recalcula a comissão liberada."""
    total = max(0, min(int(total_recebido_cent), pag.valor_cent))
    pag.valor_recebido_cent = total
    pag.pago_em = (data or hoje()) if total > 0 else None
    c = pag.comissao
    if c:
        c.valor_liberado_cent = round(total * c.pct / 100)
    return pag


def pagar_comissao(com, valor_cent=None):
    """Repassa ao vendedor. Padrão: tudo que está liberado e ainda não foi pago."""
    a_pagar = com.a_pagar_cent
    v = a_pagar if valor_cent is None else max(0, min(int(valor_cent), a_pagar))
    if v > 0:
        com.valor_pago_cent += v
        com.pago_em = hoje()
    return v


def cancelar_mensalidade(venda):
    """Cliente não quer mais manter a hospedagem: para de gerar e remove só as futuras sem recebimento.
    Mensalidades vencidas ou já recebidas ficam (e suas comissões também)."""
    venda.mensal_ativa = False
    n = 0
    for p in list(venda.pagamentos):
        if p.tipo == "mensalidade" and p.valor_recebido_cent == 0 and p.vence_em > hoje():
            db.session.delete(p)
            n += 1
    return n


def cliente_de_lead(lead):
    c = db.session.scalar(select(Cliente).where(Cliente.lead_id == lead.id))
    if c:
        return c
    c = Cliente(owner_id=lead.owner_id, lead_id=lead.id, empresa=lead.empresa, contato=lead.contato,
                telefone=lead.telefone, email=lead.email, site=lead.site, origem="lead")
    db.session.add(c)
    db.session.flush()
    return c


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS: validação, dedupe, Serper, Groq, PDF de proposta
# ═══════════════════════════════════════════════════════════════════════════
LEAD_ORIGENS = ("serper", "manual", "indicacao")
TERMO_VERSAO = 1


def termo_portal(loja):
    """Texto-modelo (LGPD). Peça para um advogado revisar antes de usar com clientes reais."""
    return (f"POLÍTICA DE PRIVACIDADE E TERMO DE USO — {loja}\n\n"
            f"1. Quem cuida dos seus dados\nA {loja} é a responsável (controladora) pelos dados tratados nesta área. "
            "Para falar sobre seus dados, use o contato da própria loja.\n\n"
            "2. Quais dados\nNome, telefone, e-mail, endereço, data de nascimento, convênio, receitas e medidas de "
            "visão (dados de saúde), pedidos, pagamentos, mensagens e os arquivos que você enviar.\n\n"
            "3. Para quê\nAtender você, produzir e entregar seus óculos, avisar sobre retirada, vencimento de receita e "
            "consultas, e cuidar de cobranças. Os dados de saúde são usados só para isso e com o seu consentimento.\n\n"
            "4. Com quem compartilhamos\nSomente com quem precisa para o serviço (por exemplo, laboratório de lentes) e "
            "com os provedores de tecnologia que armazenam as informações com segurança. Não vendemos seus dados.\n\n"
            "5. Segurança e guarda\nSeu acesso é por um link pessoal e privado: não compartilhe. Guardamos os dados "
            "enquanto houver relação com a loja ou obrigação legal.\n\n"
            "6. Seus direitos\nVocê pode ver, corrigir, pedir cópia ou exclusão dos seus dados e retirar este "
            "consentimento a qualquer momento, falando com a loja. Nesta área você já pode ver e corrigir seus dados.\n\n"
            "Ao marcar “li e concordo”, você aceita este termo e autoriza o tratamento descrito acima.")


FORMAS_PG = ("pix", "dinheiro", "debito", "credito", "boleto", "outro")
EXT_ANEXO = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
MAX_ANEXOS_REGISTRO = 10


def _assinatura_ok(ext, cab):
    """Confere os primeiros bytes: não basta o nome terminar em .pdf."""
    if ext == ".pdf":
        return cab.startswith(b"%PDF")
    if ext == ".png":
        return cab.startswith(b"\x89PNG")
    if ext in (".jpg", ".jpeg"):
        return cab.startswith(b"\xff\xd8\xff")
    if ext == ".webp":
        return cab[:4] == b"RIFF" and cab[8:12] == b"WEBP"
    return False


def taxas_da_conta(conta):
    """Taxas em % (float). Qualquer chave ausente = 0. Chaves: debito, c1..c12."""
    base = {"debito": 0.0, **{f"c{i}": 0.0 for i in range(1, 13)}}
    for k, v in ((conta.taxas if conta else None) or {}).items():
        if k in base and isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 100:
            base[k] = float(v)
    return base


def taxa_do_pagamento(conta, forma, parcelas, valor_cent):
    t = taxas_da_conta(conta)
    if forma == "debito":
        pct = t["debito"]
    elif forma == "credito":
        pct = t[f"c{min(max(int(parcelas or 1), 1), 12)}"]
    else:
        pct = 0.0
    return round(valor_cent * pct / 100)


EXT_PERMITIDAS = {".pdf", ".png", ".jpg", ".jpeg", ".docx", ".xlsx", ".pptx", ".txt", ".csv", ".zip", ".mp4"}


def _txt(d, campo, maximo, obrigatorio=False, nome=None):
    v = d.get(campo)
    if v is None or (isinstance(v, str) and not v.strip()):
        if obrigatorio:
            raise ValueError(f"{nome or campo} é obrigatório.")
        return None
    if not isinstance(v, str):
        raise ValueError(f"{nome or campo} inválido.")
    v = v.strip()
    if len(v) > maximo:
        raise ValueError(f"{nome or campo} passa de {maximo} caracteres.")
    return v


def dedupe_key(site, telefone):
    s = (site or "").strip().lower()
    if s:
        s = re.sub(r"^[a-z]+://", "", s)
        s = s.split("/")[0].split("?")[0].split("#")[0]
        s = re.sub(r"^www\.", "", s)
        if "." in s:
            return s[:300]
    dig = re.sub(r"\D", "", telefone or "")
    if dig.startswith("55") and len(dig) > 11:
        dig = dig[2:]
    return dig if len(dig) >= 8 else None


def _http_json(url, payload, headers, timeout=20):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError("Chave de API recusada. Confira em Perfil.")
        if e.code == 429:
            raise RuntimeError("Limite do provedor atingido. Tente mais tarde.")
        raise RuntimeError(f"O provedor respondeu erro {e.code}.")
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise RuntimeError("Não consegui falar com o provedor. Tente de novo.")


def serper_places(chave, consulta, cidade=""):
    q = f"{consulta} {cidade}".strip()
    data = _http_json("https://google.serper.dev/places", {"q": q, "gl": "br", "hl": "pt-br"}, {"X-API-KEY": chave})
    return data.get("places") or []


def groq_texto(chave, prompt, system):
    data = _http_json("https://api.groq.com/openai/v1/chat/completions",
                      {"model": "llama-3.3-70b-versatile", "temperature": 0.6, "max_tokens": 400,
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]},
                      {"Authorization": f"Bearer {chave}"})
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("Resposta inesperada da IA.")


def _brl(cent):
    return "R$ " + f"{cent / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def gerar_pdf_proposta(p, empresa, servico_nome, vendedor):
    """Gera o PDF em DATA_DIR/propostas e devolve o caminho relativo."""
    pasta = DATA_DIR / "propostas"
    pasta.mkdir(parents=True, exist_ok=True)
    rel = f"propostas/proposta_{p.id}.pdf"
    linhas = [("Proposta Comercial", 20), (f"Leanttro Tecnologia · Proposta #{p.id}", 10), ("", 10),
              (f"Para: {empresa}", 13), (f"Serviço: {servico_nome or 'Sob medida'}", 12),
              (f"Setup (pagamento único): {_brl(p.valor_setup_cent)}", 12),
              (f"Mensalidade: {_brl(p.valor_mensal_cent)}", 12),
              (f"Validade: {p.validade.strftime('%d/%m/%Y') if p.validade else 'a combinar'}", 12), ("", 10),
              ("Condições:", 12)] + [(t, 11) for t in (p.condicoes or "A combinar.").splitlines()] + \
             [("", 10), (f"Responsável: {vendedor.nome} · {vendedor.email}", 11)]
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        c = canvas.Canvas(str(DATA_DIR / rel), pagesize=A4)
        y = 800
        for t, tam in linhas:
            c.setFont("Helvetica-Bold" if tam >= 13 else "Helvetica", tam)
            c.drawString(56, y, t[:95])
            y -= tam + 9
        c.save()
    except ImportError:  # fallback sem dependência (texto simples, Latin-1)
        def esc_(s):
            return s.encode("latin-1", "replace").decode("latin-1").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        y, cont = 800, []
        for t, tam in linhas:
            cont.append(f"BT /F1 {tam} Tf 56 {y} Td ({esc_(t[:95])}) Tj ET")
            y -= tam + 9
        stream = "\n".join(cont).encode("latin-1")
        objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
                b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
                b"<</Length %d>>\nstream\n" % len(stream) + stream + b"\nendstream",
                b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica/Encoding/WinAnsiEncoding>>"]
        out, offs = b"%PDF-1.4\n", []
        for i, o in enumerate(objs, 1):
            offs.append(len(out))
            out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
        x = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
        out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, x)
        (DATA_DIR / rel).write_bytes(out)
    return rel


# ═══════════════════════════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════════════════════════
def _norm(email):
    return (email or "").strip().lower()


def _centavos(v, nome):
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ValueError(f"{nome} inválido.")
    return v


def _pct(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 100:
        raise ValueError("Percentual deve estar entre 0 e 100.")
    return float(v)


def _data(v):
    try:
        return date.fromisoformat(v) if v else None
    except (TypeError, ValueError):
        raise ValueError("Data inválida (use AAAA-MM-DD).")


def limitar(chave, maximo, janela_s):
    """Rate limit simples no banco. True = pode seguir."""
    agora_ts = time.time()
    db.session.execute(delete(RateHit).where(RateHit.chave == chave, RateHit.ts < agora_ts - janela_s))
    n = db.session.scalar(select(func.count()).select_from(RateHit).where(RateHit.chave == chave)) or 0
    if n >= maximo:
        db.session.commit()
        return False
    db.session.add(RateHit(chave=chave, ts=agora_ts))
    db.session.commit()
    return True


def usuario_atual():
    if "_u" in g:
        return g._u
    u, uid = None, session.get("uid")
    if uid:
        u = db.session.get(User, uid)
        if not u or u.status != "ativo" or u.sessao_versao != session.get("sv"):
            u = None
            session.pop("uid", None)
    if u and u.conta_id:                                  # conta suspensa (ou apagada) = ninguém dela entra
        c = db.session.get(Conta, u.conta_id)
        if not c or not c.ativo:
            u = None
            session.pop("uid", None)
    g._u = u
    return u


def escopo(stmt, coluna, equipe=False):
    """Isolamento no BACKEND: vendedor só vê o que é dele. Admin vê o próprio por padrão
    (perfil único = também vendedor); passa equipe=True para ver todos."""
    u = usuario_atual()
    if u.is_admin and equipe:
        return stmt
    return stmt.where(coluna == u.id)


def registrar_uso(user, servico, qtd=1):
    db.session.add(ApiUso(user_id=user.id, servico=servico, qtd=qtd))


def chave_do_vendedor(user, servico):
    """Devolve a chave em texto puro (uso interno ao chamar Groq/Serper). Nunca mandar ao front."""
    return decifrar(user.groq_key_enc if servico == "groq" else user.serper_key_enc)


def _user_json(u):
    return dict(id=u.id, nome=u.nome, email=u.email, papel=u.papel, status=u.status, telefone=u.telefone,
                comissao_setup_pct=u.comissao_setup_pct, comissao_mensal_pct=u.comissao_mensal_pct, conta_id=u.conta_id,
                groq=mascarar(u.groq_key_enc), serper=mascarar(u.serper_key_enc),
                ultimo_login=u.ultimo_login.isoformat() if u.ultimo_login else None)


def _com_json(c):
    return dict(id=c.id, pagamento_id=c.pagamento_id, venda_id=c.venda_id, vendedor_id=c.vendedor_id, pct=c.pct,
                previsto_cent=c.valor_previsto_cent, liberado_cent=c.valor_liberado_cent,
                pago_cent=c.valor_pago_cent, a_pagar_cent=c.a_pagar_cent, status=c.status)


def _aplicar_chaves(u, d):
    """None/ausente = mantém; '' = remove; texto = substitui."""
    for campo, col in (("groq", "groq_key_enc"), ("serper", "serper_key_enc")):
        v = d.get(campo)
        if v is None:
            continue
        if not isinstance(v, str) or len(v) > 300:
            raise ValueError("Chave inválida.")
        setattr(u, col, cifrar(v.strip()))


# ═══════════════════════════════════════════════════════════════════════════
# NICHOS: manifestos (nichos/<nicho>/nicho.json), validação e regras genéricas
#   Sem conta  → CRM de agência (o original). Com conta → painel do nicho da conta.
#   O admin cria a Conta (nome + nicho) e os acessos dela em /admin/contas.
# Todos os manifestos são lidos e validados na subida. Nicho com erro NÃO derruba o app:
# fica de fora da lista, o motivo aparece em /admin/contas e no log.
# ═══════════════════════════════════════════════════════════════════════════
TIPOS_CAMPO = ("texto", "textolongo", "numero", "dinheiro", "data", "select", "booleano")
MODULOS_AGENCIA = ("vendas", "prospeccao", "projetos", "financeiro", "equipe", "ia")
MODULOS_VALIDOS = MODULOS_AGENCIA + ("registros",)
# prefixos de rota de cada módulo: módulo desligado = rota responde 404
MODULO_ROTAS = {
    "vendas": ("/api/leads", "/api/propostas", "/api/vendas", "/api/clientes", "/api/servicos", "/api/comissoes",
               "/api/admin/servicos", "/api/admin/vendas", "/api/admin/pagamentos", "/api/admin/comissoes",
               "/api/admin/config"),
    "prospeccao": ("/api/prospeccao",),
    "projetos": ("/api/projetos",),
    "financeiro": ("/api/admin/custos", "/api/admin/financeiro"),
    "equipe": ("/api/admin/candidatos", "/api/treinamento", "/api/admin/treinamento", "/api/materiais",
               "/api/admin/materiais"),
    "ia": ("/api/ia", "/api/n/ia"),
    "registros": ("/api/n",),
}
CHAVE_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
CHAVES_CLIENTE_FIXAS = {"id", "nome", "telefone", "email", "documento", "endereco", "observacoes", "dados"}
MANIFESTO_AGENCIA = dict(
    id="agencia", nome="Leanttro CRM", cor="#7943e2", modulos=list(MODULOS_AGENCIA), visibilidade="proprio",
    rotulos=dict(cliente="Cliente", clientes="Clientes", atendente="Vendedor"),
    campos_cliente=[], tipos_registro=[], mensagens={}, ia=dict(persona=""))


def _validar_campos(lista, onde, reservadas=()):
    """Valida e normaliza a lista de campos de um manifesto. Levanta ValueError dizendo ONDE está o erro."""
    if lista is None:
        return []
    if not isinstance(lista, list):
        raise ValueError(f"{onde}: 'campos' deve ser uma lista.")
    saida, vistos = [], set()
    for i, c in enumerate(lista, 1):
        ref = f"{onde}, campo #{i}"
        if not isinstance(c, dict):
            raise ValueError(f"{ref}: deve ser um objeto.")
        chave, tipo = c.get("chave"), c.get("tipo", "texto")
        if not isinstance(chave, str) or not CHAVE_RE.match(chave):
            raise ValueError(f"{ref}: 'chave' inválida (minúsculas, números e _, começando por letra).")
        if chave in vistos or chave in reservadas:
            raise ValueError(f"{ref}: a chave '{chave}' está repetida ou é reservada.")
        if tipo not in TIPOS_CAMPO:
            raise ValueError(f"{ref} ('{chave}'): tipo '{tipo}' inválido. Use: {', '.join(TIPOS_CAMPO)}.")
        novo = dict(chave=chave, tipo=tipo, rotulo=str(c.get("rotulo") or chave.replace("_", " ").capitalize())[:80],
                    obrigatorio=bool(c.get("obrigatorio")))
        if tipo == "select":
            op = c.get("opcoes")
            if not isinstance(op, list) or not op or not all(isinstance(x, str) and x for x in op) or len(set(op)) != len(op):
                raise ValueError(f"{ref} ('{chave}'): 'opcoes' deve ser uma lista de textos únicos.")
            novo["opcoes"] = op
        for k in ("min", "max", "passo"):
            if k in c:
                if isinstance(c[k], bool) or not isinstance(c[k], (int, float)):
                    raise ValueError(f"{ref} ('{chave}'): '{k}' deve ser número.")
                novo[k] = c[k]
        novo["portal"] = bool(c.get("portal"))
        novo["interno"] = bool(c.get("interno"))
        for k in ("ajuda", "grupo", "placeholder"):
            if isinstance(c.get(k), str) and c[k].strip():
                novo[k] = c[k].strip()[:120]
        if "padrao" in c:
            try:
                novo["padrao"] = _valida_valor(novo, c["padrao"])
            except ValueError as e:
                raise ValueError(f"{ref} ('{chave}'): padrão inválido ({e})")
        vistos.add(chave)
        saida.append(novo)
    return saida


def _validar_manifesto(m):
    """Devolve o manifesto normalizado (com todos os defaults preenchidos) ou levanta ValueError."""
    if not isinstance(m, dict):
        raise ValueError("o arquivo deve conter um objeto JSON.")
    nid = m.get("id")
    if not isinstance(nid, str) or not re.fullmatch(r"[a-z0-9_-]{1,40}", nid):
        raise ValueError("'id' é obrigatório (minúsculas, números, - e _).")
    mods = m.get("modulos", ["registros", "ia"])
    if not isinstance(mods, list) or any(x not in MODULOS_VALIDOS for x in mods):
        raise ValueError(f"'modulos' aceita apenas: {', '.join(MODULOS_VALIDOS)}.")
    rotulos = dict(cliente="Cliente", clientes="Clientes", atendente="Atendente")
    if isinstance(m.get("rotulos"), dict):
        rotulos.update({k: v.strip()[:40] for k, v in m["rotulos"].items() if isinstance(v, str) and v.strip()})
    vis = m.get("visibilidade", "equipe")
    if vis not in ("equipe", "proprio"):
        raise ValueError("'visibilidade' deve ser 'equipe' (todos veem tudo) ou 'proprio'.")
    campos_cliente = _validar_campos(m.get("campos_cliente"), "campos_cliente", CHAVES_CLIENTE_FIXAS)

    tipos, ids = [], set()
    if m.get("tipos_registro") is not None and not isinstance(m["tipos_registro"], list):
        raise ValueError("'tipos_registro' deve ser uma lista.")
    for i, t in enumerate(m.get("tipos_registro") or [], 1):
        if not isinstance(t, dict):
            raise ValueError(f"tipos_registro #{i}: deve ser um objeto.")
        tid = t.get("id")
        if not isinstance(tid, str) or not CHAVE_RE.match(tid) or tid in ids:
            raise ValueError(f"tipos_registro #{i}: 'id' inválido ou repetido.")
        ids.add(tid)
        onde = f"tipo '{tid}'"
        campos = _validar_campos(t.get("campos"), onde)
        por_chave = {c["chave"]: c for c in campos}
        etapas = t.get("etapas") or ["Aberto", "Concluído"]
        if (not isinstance(etapas, list) or not etapas or len(set(etapas)) != len(etapas)
                or not all(isinstance(e, str) and e.strip() and len(e) <= 30 for e in etapas)):
            raise ValueError(f"{onde}: 'etapas' deve ser lista de textos únicos de até 30 caracteres.")
        finais = t.get("etapas_finais") or [etapas[-1]]
        if not isinstance(finais, list) or not all(f in etapas for f in finais):
            raise ValueError(f"{onde}: 'etapas_finais' deve conter apenas itens de 'etapas'.")

        def ligar(nome, aceitos):
            k = t.get(nome)
            if k is None:
                return None
            if k not in por_chave or por_chave[k]["tipo"] not in aceitos:
                raise ValueError(f"{onde}: '{nome}' deve apontar para um campo do tipo {'/'.join(aceitos)}.")
            return k

        data_ref, valor, titulo = ligar("data_ref", ("data",)), ligar("valor", ("dinheiro",)), ligar("titulo", ("texto", "select"))
        pg = t.get("pagamentos")
        if not pg:
            pg = None
        elif pg is True:
            pg = dict(sinal_pct=None)
        elif isinstance(pg, dict):
            sp = pg.get("sinal_pct")
            if sp is not None and (isinstance(sp, bool) or not isinstance(sp, (int, float)) or not 0 < sp < 100):
                raise ValueError(f"{onde}: pagamentos.sinal_pct deve estar entre 0 e 100.")
            if sp is not None and not valor:
                raise ValueError(f"{onde}: 'sinal_pct' exige o apontamento 'valor' (campo dinheiro).")
            pg = dict(sinal_pct=sp)
        else:
            raise ValueError(f"{onde}: 'pagamentos' deve ser true ou um objeto.")
        lems = t.get("lembretes") if t.get("lembretes") is not None else ([t["lembrete"]] if t.get("lembrete") else [])
        if not isinstance(lems, list):
            raise ValueError(f"{onde}: 'lembretes' deve ser uma lista.")
        lembretes = []
        for j, lem in enumerate(lems, 1):
            if not isinstance(lem, dict) or lem.get("campo") not in por_chave or por_chave[lem["campo"]]["tipo"] != "data":
                raise ValueError(f"{onde}, lembrete #{j}: 'campo' deve apontar para um campo do tipo data.")
            dias = lem.get("dias_antes", 0)
            if isinstance(dias, bool) or not isinstance(dias, int) or not 0 <= dias <= 365:
                raise ValueError(f"{onde}, lembrete #{j}: 'dias_antes' deve ser inteiro de 0 a 365.")
            lembretes.append(dict(campo=lem["campo"], dias_antes=dias,
                                  titulo=str(lem.get("titulo") or f"{t.get('rotulo') or tid}: {{titulo}} — {{nome}}")[:200]))
        tipos.append(dict(id=tid, anexos=bool(t.get("anexos")), portal=t.get("portal", True) is not False, rotulo=str(t.get("rotulo") or tid)[:60],
                          rotulo_plural=str(t.get("rotulo_plural") or (t.get("rotulo") or tid) + "s")[:60],
                          campos=campos, etapas=etapas, etapas_finais=finais, data_ref=data_ref, valor=valor,
                          titulo=titulo, pagamentos=pg, lembretes=lembretes))
    if "registros" in mods and not tipos:
        raise ValueError("o módulo 'registros' precisa de ao menos um item em 'tipos_registro'.")
    msgs = m.get("mensagens") or {}
    if not isinstance(msgs, dict) or not all(CHAVE_RE.match(str(k)) and isinstance(v, str) for k, v in msgs.items()):
        raise ValueError("'mensagens' deve mapear chave → texto (a chave usa minúsculas, números e _).")
    ia = m.get("ia") or {}
    if not isinstance(ia, dict) or not isinstance(ia.get("persona", ""), str):
        raise ValueError("'ia' deve ser um objeto com 'persona' em texto.")
    cor = m.get("cor", "#7943e2")
    if not isinstance(cor, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", cor):
        raise ValueError("'cor' deve ser um hexadecimal como #7943e2.")
    return dict(id=nid, nome=str(m.get("nome") or nid)[:60], cor=cor, modulos=list(mods), visibilidade=vis, rotulos=rotulos,
                campos_cliente=campos_cliente, tipos_registro=tipos, mensagens=msgs,
                ia=dict(persona=ia.get("persona", "")[:300]))


def carregar_todos():
    """Lê todo nichos/*/nicho.json → ({id: manifesto}, {pasta: erro}). 'agencia' sempre existe."""
    ok, erros = {"agencia": MANIFESTO_AGENCIA}, {}
    if NICHOS_DIR.is_dir():
        for pasta in sorted(NICHOS_DIR.iterdir()):
            arq = pasta / "nicho.json"
            if not pasta.is_dir() or not arq.is_file():
                continue
            try:
                m = _validar_manifesto(json.loads(arq.read_text("utf-8")))
                if m["id"] != pasta.name:
                    raise ValueError(f"o 'id' ({m['id']}) precisa ser igual ao nome da pasta ({pasta.name}).")
                ok[m["id"]] = m
            except (ValueError, OSError) as e:          # JSON quebrado também cai aqui (JSONDecodeError é ValueError)
                erros[pasta.name] = str(e)
                print(f"AVISO: nicho '{pasta.name}' ignorado: {e}", file=sys.stderr)
    return ok, erros


def _valida_valor(c, v):
    """Valida UM valor contra a definição do campo. Vazio → None (campo some do JSON)."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    t, nome = c["tipo"], c["rotulo"]
    if t in ("texto", "textolongo"):
        if not isinstance(v, str):
            raise ValueError(f"{nome} inválido.")
        v = v.strip()
        lim = int(c.get("max") or (200 if t == "texto" else 4000))
        if len(v) > lim:
            raise ValueError(f"{nome} passa de {lim} caracteres.")
        return v
    if t == "numero":
        if isinstance(v, str):
            try:
                v = float(v.strip().replace(",", "."))
            except ValueError:
                raise ValueError(f"{nome} deve ser um número.")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"{nome} deve ser um número.")
        if "min" in c and v < c["min"]:
            raise ValueError(f"{nome} não pode ser menor que {c['min']}.")
        if "max" in c and v > c["max"]:
            raise ValueError(f"{nome} não pode ser maior que {c['max']}.")
        return int(v) if float(v).is_integer() else v
    if t == "dinheiro":
        return _centavos(v, nome)
    if t == "data":
        return _data(v).isoformat()
    if t == "select":
        if v not in c["opcoes"]:
            raise ValueError(f"{nome}: opção inválida.")
        return v
    if t == "booleano":
        if not isinstance(v, bool):
            raise ValueError(f"{nome} deve ser sim ou não.")
        return v
    raise ValueError(f"{nome}: tipo desconhecido.")


def validar_dados(campos, entrada, atual=None):
    """Valida `entrada` contra `campos` e devolve um NOVO dict = atual + o que foi enviado.
    - chave fora do manifesto é ignorada (nada de lixo no JSON);
    - campo que saiu do manifesto continua guardado (nunca se apaga dado do cliente);
    - valor vazio remove o campo."""
    base = dict(atual or {})
    entrada = entrada if isinstance(entrada, dict) else {}
    for c in campos:
        if c["chave"] in entrada:
            v = _valida_valor(c, entrada[c["chave"]])
            if v is None:
                base.pop(c["chave"], None)
            else:
                base[c["chave"]] = v
    for c in campos:
        if c["obrigatorio"] and base.get(c["chave"]) in (None, ""):
            raise ValueError(f"{c['rotulo']} é obrigatório.")
    if len(json.dumps(base, ensure_ascii=False)) > 20000:
        raise ValueError("Dados grandes demais.")
    return base


def padroes(campos):
    return {c["chave"]: c["padrao"] for c in campos if c.get("padrao") is not None}


def _int_id(v, nome):
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{nome} inválido.")
    return v


def agora_local():
    return datetime.now(TZ).replace(tzinfo=None)


def _preencher(modelo, ctx):
    """{chave} → valor. Só substitui chaves simples (sem atributos), o que evita vazar dados por template."""
    return re.sub(r"\{(\w+)\}", lambda m: str(ctx.get(m.group(1), "")), modelo)


def _data_br(d):
    if not d:
        return ""
    d = d if isinstance(d, date) else date.fromisoformat(d)
    return d.strftime("%d/%m/%Y")


def _fmt_campo(c, v):
    if v is None or v == "":
        return ""
    t = c["tipo"]
    if t == "dinheiro":
        return _brl(v)
    if t == "data":
        return _data_br(v)
    if t == "booleano":
        return "sim" if v else "não"
    if t == "numero":
        return str(v).replace(".", ",")
    return str(v)


def dados_legiveis(campos, dados):
    return {c["chave"]: _fmt_campo(c, (dados or {}).get(c["chave"])) for c in campos}


def wa_link(telefone, texto):
    dig = re.sub(r"\D", "", telefone or "")
    if len(dig) in (10, 11):
        dig = "55" + dig
    if len(dig) < 12:
        return None
    return "https://wa.me/" + dig + "?text=" + urllib.parse.quote(texto)


def derivar_colunas(r, t):
    """Copia do JSON para as colunas que servem para filtrar/ordenar/somar. O JSON continua sendo a fonte."""
    d = r.dados or {}
    r.titulo = str((d.get(t["titulo"]) if t["titulo"] else None) or t["rotulo"])[:200]
    r.valor_cent = int(d.get(t["valor"]) or 0) if t["valor"] else 0
    ref = d.get(t["data_ref"]) if t["data_ref"] else None
    r.data_ref = date.fromisoformat(ref) if ref else (r.data_ref or hoje())


def gerar_sinal(r, pct):
    sinal = round(r.valor_cent * pct / 100)
    db.session.add(RegistroPagamento(registro_id=r.id, descricao=f"Sinal ({pct:g}%)", valor_cent=sinal, vence_em=hoje()))
    if r.valor_cent - sinal > 0:
        db.session.add(RegistroPagamento(registro_id=r.id, descricao="Restante", valor_cent=r.valor_cent - sinal,
                                         vence_em=r.data_ref or hoje()))


def sincronizar_lembretes(r, t, cliente):
    """Mantém as Atividades automáticas do registro de acordo com o manifesto (idempotente).
    Data mudou → atualiza (e reabre se já estava feita). Data apagada ou etapa final → remove a pendente."""
    for i, lem in enumerate(t["lembretes"]):
        chave = f"auto:{t['id']}:{lem['campo']}:{i}"
        alvo = (r.dados or {}).get(lem["campo"])
        atual = db.session.scalar(select(Atividade).where(Atividade.registro_id == r.id, Atividade.auto_chave == chave))
        if not alvo or r.status in t["etapas_finais"]:
            if atual and not atual.feita_em:
                db.session.delete(atual)
            continue
        quando = datetime.combine(date.fromisoformat(alvo) - timedelta(days=lem["dias_antes"]), datetime.min.time()).replace(hour=9)
        titulo = _preencher(lem["titulo"], dict(titulo=r.titulo, nome=cliente.empresa))[:200]
        if atual:
            if atual.vence_em != quando:
                atual.vence_em, atual.feita_em = quando, None
            if not atual.feita_em:
                atual.titulo = titulo
        else:
            db.session.add(Atividade(user_id=r.owner_id, cliente_id=cliente.id, registro_id=r.id, titulo=titulo,
                                     vence_em=quando, auto_chave=chave))


NICHOS, NICHOS_ERRO = carregar_todos()
MODULOS_EM_CONTA = {"registros", "ia"}      # contas de cliente nunca enxergam módulos de agência (dados globais)


def nicho_serve_conta(m):
    return "registros" in m["modulos"] and set(m["modulos"]) <= MODULOS_EM_CONTA


def conta_do(u):
    if not u or not u.conta_id:
        return None
    if "_conta" not in g:
        g._conta = db.session.get(Conta, u.conta_id)
    return g._conta


def manifesto_do(u):
    """Manifesto do painel desta pessoa. Sem conta = agência. None = nicho da conta sumiu ou está inválido."""
    c = conta_do(u)
    return NICHOS["agencia"] if not c else NICHOS.get(c.nicho)


def modulos_do(u):
    m = manifesto_do(u)
    if m is None:
        return set()
    mods = set(m["modulos"])
    return mods & MODULOS_EM_CONTA if (u and u.conta_id) else mods


def nome_app_do(u):
    c = conta_do(u)
    return c.nome if c else NICHOS["agencia"]["nome"]


def migrar():
    """Colunas novas em tabelas que já existiam (db.create_all só cria tabelas que faltam). Idempotente."""
    _add_col("users", "conta_id", "INTEGER REFERENCES contas(id)")
    _add_col("clientes", "dados", "JSON")
    _add_col("clientes", "conta_id", "INTEGER REFERENCES contas(id)")
    _add_col("registros", "conta_id", "INTEGER REFERENCES contas(id)")
    _add_col("atividades", "registro_id", "INTEGER REFERENCES registros(id) ON DELETE CASCADE")
    _add_col("atividades", "auto_chave", "VARCHAR(80)")
    _add_col("contas", "taxas", "JSON")
    _add_col("clientes", "portal_hash", "VARCHAR(64)")
    _add_col("clientes", "portal_token_enc", "TEXT")
    _add_col("clientes", "portal_aceite_em", "DATETIME")
    _add_col("clientes", "portal_aceite_versao", "INTEGER")
    _add_col("clientes", "portal_ultimo_acesso", "DATETIME")
    db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_clientes_portal_hash ON clientes(portal_hash)"))
    db.session.commit()
    _add_col("registro_pagamentos", "forma", "VARCHAR(12)")
    _add_col("registro_pagamentos", "parcelas", "INTEGER")
    _add_col("registro_pagamentos", "taxa_cent", "INTEGER NOT NULL DEFAULT 0")


def criar_app():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=_segredo("SECRET_KEY"),
        SQLALCHEMY_DATABASE_URI=_db_url(),
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_env("COOKIE_SECURE") == "1",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
        MAX_CONTENT_LENGTH=_int("MAX_UPLOAD_MB", 10) * 1024 * 1024,
    )
    app.json.ensure_ascii = False
    if _env("TRUST_PROXY") == "1":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    db.init_app(app)

    @app.template_filter("reais")
    def _reais(c):
        s = f"{(c or 0) / 100:,.2f}"
        return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")

    @app.context_processor
    def _ctx():
        u = usuario_atual()
        return dict(csrf=session.get("csrf", ""), usuario=u, app_nome=nome_app_do(u), nicho=manifesto_do(u))

    @app.before_request
    def _csrf():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not request.path.startswith("/api/c/"):
            enviado = request.headers.get("X-CSRF") or request.form.get("csrf", "")
            if not hmac.compare_digest(enviado, session["csrf"]):
                if request.path.startswith("/api/"):
                    return jsonify(ok=False, erro="Sessão expirada. Recarregue a página."), 400
                return render_template("login.html", erro="Sua sessão expirou. Tente entrar de novo."), 400

    @app.before_request
    def _modulos():
        """Cada pessoa só alcança as rotas dos módulos do seu painel (agência ou nicho da conta); o resto responde 404."""
        p = request.path
        u = usuario_atual() if p.startswith("/api/") else None
        if not u:
            return None
        if manifesto_do(u) is None:
            return jsonify(ok=False, erro="Painel indisponível: o nicho desta conta não está carregado. Fale com o administrador."), 503
        mods = modulos_do(u)
        for mod, prefixos in MODULO_ROTAS.items():
            if mod not in mods and any(p == x or p.startswith(x + "/") for x in prefixos):
                return jsonify(ok=False, erro="Recurso não disponível neste painel."), 404

    @app.after_request
    def _headers(r):
        r.headers["X-Content-Type-Options"] = "nosniff"
        r.headers["X-Frame-Options"] = "DENY"
        r.headers["Referrer-Policy"] = "no-referrer" if request.path.startswith(("/c/", "/api/c/")) else "same-origin"
        if request.path.startswith(("/c/", "/api/c/")):
            r.headers["X-Robots-Tag"] = "noindex, nofollow"
        r.headers["Content-Security-Policy"] = ("default-src 'self' 'unsafe-inline' data: blob:; "
                                                "img-src 'self' data: blob: https://res.cloudinary.com; "
                                                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                                                "font-src 'self' data: https://fonts.gstatic.com; "
                                                "connect-src 'self'; frame-ancestors 'none'")
        if request.path.startswith(("/api/", "/app", "/login", "/conta", "/c/")):
            r.headers["Cache-Control"] = "no-store"
        return r

    def login_obrigatorio(f):
        @wraps(f)
        def w(*a, **k):
            u = usuario_atual()
            if not u:
                if request.path.startswith("/api/"):
                    return jsonify(ok=False, erro="Faça login."), 401
                return redirect("/login")
            if u.trocar_senha and request.endpoint not in ("senha", "sair"):
                if request.path.startswith("/api/"):
                    return jsonify(ok=False, erro="Troque a senha provisória primeiro."), 403
                return redirect("/conta/senha")
            return f(*a, **k)
        return w

    def admin_obrigatorio(f):
        @wraps(f)
        @login_obrigatorio
        def w(*a, **k):
            if not usuario_atual().is_admin:
                return jsonify(ok=False, erro="Acesso restrito ao administrador."), 403
            return f(*a, **k)
        return w

    def _json():
        d = request.get_json(silent=True)
        return d if isinstance(d, dict) else {}

    def _erro(msg, cod=400):
        return jsonify(ok=False, erro=msg), cod

    # ── básicas ─────────────────────────────────────────────────────────────
    @app.get("/healthz")
    def healthz():
        db.session.execute(text("SELECT 1"))
        return "ok"

    @app.get("/")
    def raiz():
        return redirect("/app" if usuario_atual() else "/login")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            return render_template("login.html", erro=None)
        email, senha = _norm(request.form.get("email")), request.form.get("senha", "")
        if not limitar(f"login:{request.remote_addr}:{email}", 8, 600):
            return render_template("login.html", erro="Muitas tentativas. Aguarde alguns minutos."), 429
        u = db.session.scalar(select(User).where(User.email_norm == email))
        if not u or not check_password_hash(u.senha_hash, senha):
            return render_template("login.html", erro="E-mail ou senha incorretos."), 401
        if u.status != "ativo":
            return render_template("login.html", erro="Acesso desativado. Fale com o administrador."), 403
        if u.conta_id:
            conta = db.session.get(Conta, u.conta_id)
            if not conta or not conta.ativo:
                return render_template("login.html", erro="Esta conta está suspensa. Fale com o administrador."), 403
        session.clear()
        session.permanent = True
        session.update(uid=u.id, sv=u.sessao_versao, csrf=secrets.token_urlsafe(24))
        u.ultimo_login = agora()
        if u.is_admin:
            manter_mensalidades_todas()
        db.session.commit()
        return redirect("/conta/senha" if u.trocar_senha else "/app")

    @app.post("/sair")
    def sair():
        session.clear()
        return redirect("/login")

    @app.get("/app")
    @login_obrigatorio
    def painel():
        u = usuario_atual()
        m = manifesto_do(u)
        if m is None:
            return "Painel indisponível: o nicho desta conta não está carregado. Fale com o administrador.", 503
        if "registros" not in modulos_do(u):                 # sem conta: CRM de agência (original)
            return render_template("app.html")
        # conta de nicho: templates/nichos/<nicho>/app.html se existir, senão o painel genérico
        return render_template([f"nichos/{m['id']}/app.html", "app_generico.html"])

    @app.route("/conta/senha", methods=["GET", "POST"])
    @login_obrigatorio
    def senha():
        u = usuario_atual()
        if request.method == "GET":
            return render_template("senha.html", erro=None)
        atual, nova = request.form.get("atual", ""), request.form.get("nova", "")
        if not check_password_hash(u.senha_hash, atual):
            return render_template("senha.html", erro="Senha atual incorreta."), 400
        if len(nova) < 8:
            return render_template("senha.html", erro="A nova senha precisa ter ao menos 8 caracteres."), 400
        u.senha_hash = generate_password_hash(nova)
        u.trocar_senha = False
        u.sessao_versao += 1                     # derruba as outras sessões
        db.session.commit()
        session["sv"] = u.sessao_versao
        return redirect("/app")

    # ── minhas chaves (vendedor altera as próprias) ────────────────────────
    @app.get("/api/me")
    @login_obrigatorio
    def api_me():
        return jsonify(ok=True, usuario=_user_json(usuario_atual()))

    @app.put("/api/me/chaves")
    @login_obrigatorio
    def api_me_chaves():
        try:
            _aplicar_chaves(usuario_atual(), _json())
        except ValueError as e:
            return _erro(str(e))
        db.session.commit()
        return jsonify(ok=True, usuario=_user_json(usuario_atual()))

    # ── admin: usuários e configuração ─────────────────────────────────────
    @app.get("/api/admin/usuarios")
    @admin_obrigatorio
    def admin_usuarios():
        return jsonify(ok=True, usuarios=[_user_json(u) for u in db.session.scalars(select(User).where(User.conta_id.is_(None)).order_by(User.nome))])

    @app.post("/api/admin/usuarios")
    @admin_obrigatorio
    def admin_criar_usuario():
        d = _json()
        nome, email = (d.get("nome") or "").strip()[:80], _norm(d.get("email"))
        papel = d.get("papel", "vendedor")
        if not nome or not EMAIL_RE.match(email) or papel not in PAPEIS:
            return _erro("Informe nome, e-mail válido e papel (admin ou vendedor).")
        temp = secrets.token_urlsafe(9)
        try:
            u = User(nome=nome, email=email, email_norm=email, papel=papel, telefone=(d.get("telefone") or "")[:30] or None,
                     comissao_setup_pct=_pct(d.get("comissao_setup_pct")),
                     comissao_mensal_pct=_pct(d.get("comissao_mensal_pct")),
                     senha_hash=generate_password_hash(temp), trocar_senha=True)
            _aplicar_chaves(u, d)
            db.session.add(u)
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        except IntegrityError:
            db.session.rollback()
            return _erro("Já existe um usuário com esse e-mail.", 409)
        return jsonify(ok=True, usuario=_user_json(u), senha_temporaria=temp)   # só aparece desta vez

    @app.put("/api/admin/usuarios/<int:uid>")
    @admin_obrigatorio
    def admin_editar_usuario(uid):
        u, d = db.session.get(User, uid), _json()
        if not u:
            abort(404)
        eu = usuario_atual()
        if u.conta_id and d.get("papel") == "admin":
            return _erro("Usuário de conta de cliente não pode ser administrador da plataforma.")
        if u.id == eu.id and (d.get("status") == "bloqueado" or d.get("papel") == "vendedor"):
            return _erro("Você não pode bloquear nem rebaixar o próprio acesso.")
        try:
            if "nome" in d and (d["nome"] or "").strip():
                u.nome = d["nome"].strip()[:80]
            if "telefone" in d:
                u.telefone = (d["telefone"] or "")[:30] or None
            if d.get("papel") in PAPEIS:
                u.papel = d["papel"]
            if d.get("status") in ("ativo", "bloqueado"):
                u.status = d["status"]
                if u.status == "bloqueado":
                    u.sessao_versao += 1
            for campo in ("comissao_setup_pct", "comissao_mensal_pct"):
                if campo in d:                      # null = volta a usar o global
                    setattr(u, campo, _pct(d[campo]))
            _aplicar_chaves(u, d)
            temp = None
            if d.get("resetar_senha"):
                temp = secrets.token_urlsafe(9)
                u.senha_hash, u.trocar_senha = generate_password_hash(temp), True
                u.sessao_versao += 1
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        db.session.commit()
        return jsonify(ok=True, usuario=_user_json(u), senha_temporaria=temp)

    @app.get("/api/admin/config")
    @admin_obrigatorio
    def admin_config_get():
        return jsonify(ok=True, config={k: float(cfg_get(k)) for k in CONFIG_PADRAO})

    @app.put("/api/admin/config")
    @admin_obrigatorio
    def admin_config_put():
        d = _json()
        try:
            for k in CONFIG_PADRAO:
                if k in d:
                    p = _pct(d[k])
                    if p is None:
                        raise ValueError("Percentual obrigatório.")
                    cfg_set(k, p)
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        db.session.commit()
        return jsonify(ok=True, config={k: float(cfg_get(k)) for k in CONFIG_PADRAO})

    # ── admin: dinheiro (recebimento, comissão, cancelamento) ──────────────
    @app.post("/api/admin/pagamentos/<int:pid>/receber")
    @admin_obrigatorio
    def admin_receber(pid):
        p, d = db.session.get(Pagamento, pid), _json()
        if not p:
            abort(404)
        try:
            receber_pagamento(p, _centavos(d.get("valor_recebido_cent"), "Valor"), _data(d.get("data")))
        except ValueError as e:
            return _erro(str(e))
        db.session.commit()
        return jsonify(ok=True, status=p.status, comissao=_com_json(p.comissao))

    @app.post("/api/admin/comissoes/<int:cid>/pagar")
    @admin_obrigatorio
    def admin_pagar_comissao(cid):
        c, d = db.session.get(Comissao, cid), _json()
        if not c:
            abort(404)
        try:
            v = pagar_comissao(c, _centavos(d["valor_cent"], "Valor") if d.get("valor_cent") is not None else None)
        except ValueError as e:
            return _erro(str(e))
        db.session.commit()
        return jsonify(ok=True, pago_agora_cent=v, comissao=_com_json(c))

    @app.post("/api/admin/vendas/<int:vid>/cancelar-mensal")
    @admin_obrigatorio
    def admin_cancelar_mensal(vid):
        v = db.session.get(Venda, vid)
        if not v:
            abort(404)
        removidas = cancelar_mensalidade(v)
        db.session.commit()
        return jsonify(ok=True, mensalidades_removidas=removidas)

    @app.get("/api/comissoes")
    @login_obrigatorio
    def api_comissoes():
        if limitar("manter_mensalidades", 1, 3600):
            manter_mensalidades_todas()
        stmt = escopo(select(Comissao), Comissao.vendedor_id, equipe=request.args.get("equipe") == "1")
        rows = list(db.session.scalars(stmt.order_by(Comissao.id.desc()).limit(500)))
        tot = dict(previsto_cent=sum(c.valor_previsto_cent for c in rows),
                   liberado_cent=sum(c.valor_liberado_cent for c in rows),
                   pago_cent=sum(c.valor_pago_cent for c in rows),
                   a_pagar_cent=sum(c.a_pagar_cent for c in rows))
        return jsonify(ok=True, totais=tot, comissoes=[_com_json(c) for c in rows])

    # ═══════════════════════════════════════════════════════════════════════
    # ROTAS NOVAS
    # ═══════════════════════════════════════════════════════════════════════
    def _fmt(dt):
        return dt.isoformat() if dt else None

    def _lead_meu(lid):
        l, u = db.session.get(Lead, lid), usuario_atual()
        if not l or (l.owner_id != u.id and not u.is_admin):
            abort(404)
        return l

    def _lead_json(l):
        return dict(id=l.id, owner_id=l.owner_id, empresa=l.empresa, contato=l.contato, telefone=l.telefone,
                    whatsapp=l.whatsapp, email=l.email, site=l.site, segmento=l.segmento, cidade=l.cidade,
                    origem=l.origem, servico_id=l.servico_id, valor_estimado_cent=l.valor_estimado_cent,
                    status=l.status, motivo_perda=l.motivo_perda, versao=l.versao,
                    proxima_atividade_em=_fmt(l.proxima_atividade_em), atualizado_em=_fmt(l.atualizado_em))

    def _preencher_lead(l, d, novo=False):
        l.empresa = _txt(d, "empresa", 160, novo or "empresa" in d, "Empresa") or l.empresa
        for campo, n in (("contato", 120), ("telefone", 40), ("whatsapp", 40), ("segmento", 80), ("cidade", 120), ("site", 300)):
            if campo in d:
                setattr(l, campo, _txt(d, campo, n))
        if "email" in d:
            e = _txt(d, "email", 254)
            if e and not EMAIL_RE.match(e):
                raise ValueError("E-mail inválido.")
            l.email = e
        if "valor_estimado_cent" in d:
            l.valor_estimado_cent = _centavos(d["valor_estimado_cent"], "Valor estimado")
        if "servico_id" in d:
            sid = d["servico_id"]
            if sid is not None and not db.session.get(Servico, sid):
                raise ValueError("Serviço inexistente.")
            l.servico_id = sid
        if "origem" in d and d["origem"] in LEAD_ORIGENS:
            l.origem = d["origem"]
        if "proxima_atividade_em" in d:
            try:
                l.proxima_atividade_em = datetime.fromisoformat(d["proxima_atividade_em"]) if d["proxima_atividade_em"] else None
            except (TypeError, ValueError):
                raise ValueError("Data da próxima atividade inválida.")
        chave = dedupe_key(l.site, l.telefone)
        if chave != l.dedupe_key:
            outro = db.session.scalar(select(Lead.id).where(Lead.dedupe_key == chave)) if chave else None
            if outro and outro != l.id:
                raise ValueError("Já existe um lead com esse site ou telefone.")
            l.dedupe_key = chave

    # ── leads ──────────────────────────────────────────────────────────────
    @app.get("/api/leads")
    @login_obrigatorio
    def api_leads():
        stmt = escopo(select(Lead), Lead.owner_id, equipe=request.args.get("equipe") == "1")
        rows = db.session.scalars(stmt.order_by(Lead.atualizado_em.desc()).limit(300))
        return jsonify(ok=True, leads=[_lead_json(l) for l in rows])

    @app.post("/api/leads")
    @login_obrigatorio
    def api_lead_criar():
        d, u = _json(), usuario_atual()
        l = Lead(owner_id=u.id, empresa="")
        try:
            _preencher_lead(l, d, novo=True)
            db.session.add(l)
            db.session.flush()
            db.session.add(LeadHistorico(lead_id=l.id, user_id=u.id, tipo="nota", texto="Lead criado"))
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e), 409 if "Já existe" in str(e) else 400)
        except IntegrityError:
            db.session.rollback()
            return _erro("Já existe um lead com esse site ou telefone.", 409)
        return jsonify(ok=True, lead=_lead_json(l))

    @app.get("/api/leads/<int:lid>")
    @login_obrigatorio
    def api_lead_ver(lid):
        l = _lead_meu(lid)
        hist = db.session.scalars(select(LeadHistorico).where(LeadHistorico.lead_id == l.id)
                                  .order_by(LeadHistorico.id.desc()).limit(50))
        props = db.session.scalars(select(Proposta).where(Proposta.lead_id == l.id).order_by(Proposta.id.desc()))
        return jsonify(ok=True, lead=_lead_json(l),
                       historico=[dict(tipo=h.tipo, texto=h.texto, de=h.status_de, para=h.status_para, em=_fmt(h.criado_em)) for h in hist],
                       propostas=[dict(id=p.id, status=p.status, setup_cent=p.valor_setup_cent, mensal_cent=p.valor_mensal_cent,
                                       tem_pdf=bool(p.pdf_path)) for p in props])

    @app.put("/api/leads/<int:lid>")
    @login_obrigatorio
    def api_lead_editar(lid):
        l, d, u = _lead_meu(lid), _json(), usuario_atual()
        if "versao" in d and d["versao"] != l.versao:
            return _erro("Este lead foi alterado por outra pessoa. Recarregue.", 409)
        try:
            antigo = l.status
            _preencher_lead(l, d)
            novo = d.get("status")
            if novo and novo != antigo:
                if novo not in LEAD_STATUS:
                    raise ValueError("Status inválido.")
                if novo == "fechado":
                    raise ValueError("Para fechar, use o botão Fechar venda.")
                if antigo == "fechado":
                    raise ValueError("Lead já fechado não pode mudar de etapa.")
                if novo == "perdido":
                    l.motivo_perda = _txt(d, "motivo_perda", 200, True, "Motivo da perda")
                l.status = novo
                db.session.add(LeadHistorico(lead_id=l.id, user_id=u.id, tipo="status", texto=l.motivo_perda or "",
                                             status_de=antigo, status_para=novo))
            l.versao += 1
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e), 409 if "Já existe" in str(e) else 400)
        return jsonify(ok=True, lead=_lead_json(l))

    @app.post("/api/leads/<int:lid>/nota")
    @login_obrigatorio
    def api_lead_nota(lid):
        l, d = _lead_meu(lid), _json()
        try:
            txt = _txt(d, "texto", 2000, True, "Texto")
        except ValueError as e:
            return _erro(str(e))
        db.session.add(LeadHistorico(lead_id=l.id, user_id=usuario_atual().id,
                                     tipo="contato" if d.get("tipo") == "contato" else "nota", texto=txt))
        l.atualizado_em = agora()
        db.session.commit()
        return jsonify(ok=True)

    @app.post("/api/leads/<int:lid>/fechar")
    @login_obrigatorio
    def api_lead_fechar(lid):
        l, d, u = _lead_meu(lid), _json(), usuario_atual()
        if l.status == "fechado":
            return _erro("Este lead já foi fechado.", 409)
        try:
            setup, mensal = _centavos(d.get("setup_cent", 0), "Setup"), _centavos(d.get("mensal_cent", 0), "Mensalidade")
            if setup + mensal <= 0:
                raise ValueError("Informe o valor do setup e/ou da mensalidade.")
            sid = d.get("servico_id") or l.servico_id
            if sid and not db.session.get(Servico, sid):
                raise ValueError("Serviço inexistente.")
            prop = db.session.get(Proposta, d["proposta_id"]) if d.get("proposta_id") else None
            if prop and prop.lead_id != l.id:
                raise ValueError("Proposta de outro lead.")
            ps, pm = (_pct(d.get("pct_setup")), _pct(d.get("pct_mensal"))) if u.is_admin else (None, None)
            vendedor = db.session.get(User, l.owner_id)
            venda = criar_venda(cliente_de_lead(l), vendedor, setup, mensal, sid, l, prop.id if prop else None,
                                setup_vence=_data(d.get("setup_vence")), mensal_inicio=_data(d.get("mensal_inicio")),
                                pct_setup=ps, pct_mensal=pm)
            if prop:
                prop.status = "aceita"
            l.versao += 1
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        return jsonify(ok=True, venda_id=venda.id, cliente_id=venda.cliente_id)

    # ── serviços ───────────────────────────────────────────────────────────
    def _srv_json(s):
        return dict(id=s.id, nome=s.nome, descricao=s.descricao, setup_cent=s.preco_setup_cent,
                    mensal_cent=s.preco_mensal_cent, ativo=s.ativo)

    @app.get("/api/servicos")
    @login_obrigatorio
    def api_servicos():
        stmt = select(Servico).order_by(Servico.nome)
        if not usuario_atual().is_admin:
            stmt = stmt.where(Servico.ativo.is_(True))
        return jsonify(ok=True, servicos=[_srv_json(s) for s in db.session.scalars(stmt)])

    @app.route("/api/admin/servicos", methods=["POST"])
    @app.route("/api/admin/servicos/<int:sid>", methods=["PUT"])
    @admin_obrigatorio
    def admin_servico(sid=None):
        d = _json()
        s = db.session.get(Servico, sid) if sid else Servico()
        if sid and not s:
            abort(404)
        try:
            if not sid or "nome" in d:
                s.nome = _txt(d, "nome", 120, True, "Nome")
            if "descricao" in d:
                s.descricao = _txt(d, "descricao", 2000) or ""
            if "setup_cent" in d:
                s.preco_setup_cent = _centavos(d["setup_cent"], "Setup")
            if "mensal_cent" in d:
                s.preco_mensal_cent = _centavos(d["mensal_cent"], "Mensalidade")
            if "ativo" in d:
                s.ativo = bool(d["ativo"])
            db.session.add(s)
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        return jsonify(ok=True, servico=_srv_json(s))

    # ── prospecção (Serper) ────────────────────────────────────────────────
    def _res_json(r):
        return dict(id=r.id, titulo=r.titulo, site=r.site, telefone=r.telefone, endereco=r.endereco, status=r.status, lead_id=r.lead_id)

    @app.post("/api/prospeccao")
    @login_obrigatorio
    def api_prospeccao():
        d, u = _json(), usuario_atual()
        try:
            consulta, cidade = _txt(d, "consulta", 200, True, "Segmento"), _txt(d, "cidade", 120)
        except ValueError as e:
            return _erro(str(e))
        chave = chave_do_vendedor(u, "serper")
        if not chave:
            return _erro("Cadastre sua chave Serper em Perfil antes de buscar.", 400)
        if not limitar(f"serper:{u.id}", 30, 3600):
            return _erro("Limite de 30 buscas por hora atingido.", 429)
        try:
            places = serper_places(chave, consulta, cidade or "")
        except RuntimeError as e:
            return _erro(str(e), 502)
        b = Busca(user_id=u.id, consulta=consulta, cidade=cidade)
        db.session.add(b)
        db.session.flush()
        for p in places[:20]:
            titulo = (p.get("title") or "").strip()
            if not titulo:
                continue
            db.session.add(BuscaResultado(busca_id=b.id, titulo=titulo[:200], site=(p.get("website") or "")[:300] or None,
                                          telefone=(p.get("phoneNumber") or "")[:40] or None,
                                          endereco=(p.get("address") or "")[:300] or None,
                                          raw_json=json.dumps(p, ensure_ascii=False)[:8000]))
        registrar_uso(u, "serper")
        db.session.commit()
        rs = db.session.scalars(select(BuscaResultado).where(BuscaResultado.busca_id == b.id).order_by(BuscaResultado.id))
        return jsonify(ok=True, busca_id=b.id, resultados=[_res_json(r) for r in rs])

    @app.get("/api/prospeccao")
    @login_obrigatorio
    def api_buscas():
        u = usuario_atual()
        bs = db.session.scalars(select(Busca).where(Busca.user_id == u.id).order_by(Busca.id.desc()).limit(10))
        return jsonify(ok=True, buscas=[dict(id=b.id, consulta=b.consulta, cidade=b.cidade, em=_fmt(b.criado_em)) for b in bs])

    @app.get("/api/prospeccao/<int:bid>")
    @login_obrigatorio
    def api_busca_ver(bid):
        b = db.session.get(Busca, bid)
        if not b or b.user_id != usuario_atual().id:
            abort(404)
        rs = db.session.scalars(select(BuscaResultado).where(BuscaResultado.busca_id == b.id).order_by(BuscaResultado.id))
        return jsonify(ok=True, busca=dict(id=b.id, consulta=b.consulta, cidade=b.cidade), resultados=[_res_json(r) for r in rs])

    def _resultado_meu(rid):
        r = db.session.get(BuscaResultado, rid)
        b = db.session.get(Busca, r.busca_id) if r else None
        if not r or b.user_id != usuario_atual().id:
            abort(404)
        return r, b

    @app.post("/api/prospeccao/resultados/<int:rid>/importar")
    @login_obrigatorio
    def api_resultado_importar(rid):
        r, b = _resultado_meu(rid)
        if r.status == "importado":
            return _erro("Esse resultado já virou lead.", 409)
        chave = dedupe_key(r.site, r.telefone)
        if chave and db.session.scalar(select(Lead.id).where(Lead.dedupe_key == chave)):
            r.status = "descartado"
            db.session.commit()
            return _erro("Já existe um lead com esse site ou telefone.", 409)
        l = Lead(owner_id=usuario_atual().id, empresa=r.titulo[:160], telefone=r.telefone, site=r.site,
                 segmento=b.consulta[:80], cidade=b.cidade, origem="serper", dedupe_key=chave)
        db.session.add(l)
        db.session.flush()
        r.status, r.lead_id = "importado", l.id
        db.session.add(LeadHistorico(lead_id=l.id, user_id=l.owner_id, tipo="nota", texto="Importado da prospecção"))
        db.session.commit()
        return jsonify(ok=True, lead=_lead_json(l))

    @app.post("/api/prospeccao/resultados/<int:rid>/descartar")
    @login_obrigatorio
    def api_resultado_descartar(rid):
        r, _ = _resultado_meu(rid)
        if r.status == "novo":
            r.status = "descartado"
            db.session.commit()
        return jsonify(ok=True)

    # ── IA (Groq) ──────────────────────────────────────────────────────────
    @app.post("/api/ia/mensagem")
    @login_obrigatorio
    def api_ia_mensagem():
        d, u = _json(), usuario_atual()
        l = _lead_meu(d.get("lead_id") or 0)
        chave = chave_do_vendedor(u, "groq")
        if not chave:
            return _erro("Cadastre sua chave Groq em Perfil.", 400)
        if not limitar(f"groq:{u.id}", 60, 3600):
            return _erro("Limite de 60 mensagens por hora atingido.", 429)
        canal = "WhatsApp" if d.get("canal") != "email" else "e-mail"
        prompt = (f"Escreva uma primeira mensagem de {canal} em português do Brasil, curta (até 90 palavras), cordial e sem exageros, "
                  f"da Leanttro Tecnologia (sites, sistemas e automações) para a empresa '{l.empresa}'"
                  f"{', segmento ' + l.segmento if l.segmento else ''}{', em ' + l.cidade if l.cidade else ''}. "
                  f"Assine como {u.nome}. Não invente dados sobre a empresa. Termine com uma pergunta simples.")
        try:
            texto = groq_texto(chave, prompt, "Você é um consultor comercial brasileiro, direto e educado.")
        except RuntimeError as e:
            return _erro(str(e), 502)
        registrar_uso(u, "groq")
        db.session.commit()
        return jsonify(ok=True, texto=texto)

    # ── propostas ──────────────────────────────────────────────────────────
    @app.post("/api/propostas")
    @login_obrigatorio
    def api_proposta_criar():
        d, u = _json(), usuario_atual()
        l = _lead_meu(d.get("lead_id") or 0)
        try:
            sid = d.get("servico_id") or l.servico_id
            srv = db.session.get(Servico, sid) if sid else None
            if sid and not srv:
                raise ValueError("Serviço inexistente.")
            setup = _centavos(d.get("setup_cent", srv.preco_setup_cent if srv else 0), "Setup")
            mensal = _centavos(d.get("mensal_cent", srv.preco_mensal_cent if srv else 0), "Mensalidade")
            if setup + mensal <= 0:
                raise ValueError("Informe o valor do setup e/ou da mensalidade.")
            p = Proposta(user_id=l.owner_id, lead_id=l.id, servico_id=sid, valor_setup_cent=setup, valor_mensal_cent=mensal,
                         condicoes=_txt(d, "condicoes", 3000) or "", validade=_data(d.get("validade")))
            db.session.add(p)
            db.session.flush()
            p.pdf_path = gerar_pdf_proposta(p, l.empresa, srv.nome if srv else None, db.session.get(User, l.owner_id))
            if l.status in ("novo", "em_contato", "qualificado"):
                db.session.add(LeadHistorico(lead_id=l.id, user_id=u.id, tipo="status", texto="Proposta gerada",
                                             status_de=l.status, status_para="proposta_enviada"))
                l.status = "proposta_enviada"
            else:
                db.session.add(LeadHistorico(lead_id=l.id, user_id=u.id, tipo="proposta", texto=f"Proposta #{p.id} gerada"))
            l.versao += 1
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        return jsonify(ok=True, proposta_id=p.id)

    @app.get("/api/propostas/<int:pid>/pdf")
    @login_obrigatorio
    def api_proposta_pdf(pid):
        p, u = db.session.get(Proposta, pid), usuario_atual()
        if not p or not p.pdf_path or (p.user_id != u.id and not u.is_admin):
            abort(404)
        return send_from_directory(DATA_DIR, p.pdf_path, mimetype="application/pdf", download_name=f"proposta_{p.id}.pdf")

    @app.put("/api/propostas/<int:pid>")
    @login_obrigatorio
    def api_proposta_status(pid):
        p, u, d = db.session.get(Proposta, pid), usuario_atual(), _json()
        if not p or (p.user_id != u.id and not u.is_admin):
            abort(404)
        if d.get("status") not in ("enviada", "recusada"):
            return _erro("Status inválido.")
        p.status = d["status"]
        db.session.commit()
        return jsonify(ok=True)

    # ── vendas / pagamentos ────────────────────────────────────────────────
    # ── vendas: listar, cadastrar manualmente, editar e excluir (excluir só admin) ──
    def _venda_minha(vid):
        v, u = db.session.get(Venda, vid), usuario_atual()
        if not v or (v.vendedor_id != u.id and not u.is_admin):
            abort(404)
        return v

    def _venda_json(v, empresa, vendedor):
        setup = next((p for p in v.pagamentos if p.tipo == "setup"), None)
        return dict(id=v.id, cliente=empresa, cliente_id=v.cliente_id, vendedor_id=v.vendedor_id, vendedor=vendedor,
                    servico_id=v.servico_id, setup_cent=v.valor_setup_cent, mensal_cent=v.valor_mensal_cent,
                    mensal_ativa=v.mensal_ativa, mensal_inicio=_fmt(v.mensal_inicio),
                    setup_vence=_fmt(setup.vence_em) if setup else None,
                    pct_setup=v.pct_setup, pct_mensal=v.pct_mensal, fechada_em=_fmt(v.fechada_em),
                    recebido_cent=sum(p.valor_recebido_cent for p in v.pagamentos))

    def _ajustar_comissao(p, pct=None):
        """Recalcula o previsto. O percentual só muda onde ainda não entrou dinheiro nem repasse."""
        c = p.comissao
        if not c:
            return
        if pct is not None and p.valor_recebido_cent == 0 and c.valor_pago_cent == 0:
            c.pct = pct
        c.valor_previsto_cent = round(p.valor_cent * c.pct / 100)

    @app.get("/api/vendas")
    @login_obrigatorio
    def api_vendas():
        stmt = escopo(select(Venda, Cliente.empresa, User.nome).join(Cliente, Cliente.id == Venda.cliente_id)
                      .join(User, User.id == Venda.vendedor_id), Venda.vendedor_id,
                      equipe=request.args.get("equipe") == "1")
        rows = db.session.execute(stmt.order_by(Venda.id.desc()).limit(200)).all()
        return jsonify(ok=True, vendas=[_venda_json(v, emp, nome) for v, emp, nome in rows])

    @app.post("/api/vendas")
    @login_obrigatorio
    def api_venda_criar():
        """Cadastro manual. Cliente existente (cliente_id) ou novo (cliente_novo={empresa,...})."""
        d, u = _json(), usuario_atual()
        try:
            setup, mensal = _centavos(d.get("setup_cent", 0), "Setup"), _centavos(d.get("mensal_cent", 0), "Mensalidade")
            if setup + mensal <= 0:
                raise ValueError("Informe o valor do setup e/ou da mensalidade.")
            sid = d.get("servico_id") or None
            if sid and not db.session.get(Servico, sid):
                raise ValueError("Serviço inexistente.")
            vendedor = u
            if u.is_admin and d.get("vendedor_id"):
                vendedor = db.session.get(User, d["vendedor_id"])
                if not vendedor or vendedor.status != "ativo":
                    raise ValueError("Vendedor inválido.")
            cid = d.get("cliente_id")
            if cid:
                if isinstance(cid, bool) or not isinstance(cid, int):
                    raise ValueError("Cliente inválido.")
                cli = db.session.get(Cliente, cid)
                if not cli or (cli.owner_id != u.id and not u.is_admin):
                    raise ValueError("Cliente não encontrado.")
            else:
                novo = d.get("cliente_novo")
                if not isinstance(novo, dict):
                    raise ValueError("Escolha um cliente ou informe os dados do novo.")
                cli = Cliente(owner_id=vendedor.id, empresa="", origem="manual")
                _preencher_cliente(cli, novo, novo=True)
                db.session.add(cli)
                db.session.flush()
            ps, pm = (_pct(d.get("pct_setup")), _pct(d.get("pct_mensal"))) if u.is_admin else (None, None)
            venda = criar_venda(cli, vendedor, setup, mensal, sid, fechada_em=_data(d.get("fechada_em")),
                                setup_vence=_data(d.get("setup_vence")), mensal_inicio=_data(d.get("mensal_inicio")),
                                pct_setup=ps, pct_mensal=pm)
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        return jsonify(ok=True, venda_id=venda.id, cliente_id=cli.id)

    @app.put("/api/vendas/<int:vid>")
    @login_obrigatorio
    def api_venda_editar(vid):
        """Vendedor edita as próprias; admin edita qualquer. Percentuais: só admin.
        Parcelas que já receberam dinheiro não são apagadas nem ficam abaixo do recebido."""
        v, d, u = _venda_minha(vid), _json(), usuario_atual()
        try:
            setup = _centavos(d.get("setup_cent", v.valor_setup_cent), "Setup")
            mensal = _centavos(d.get("mensal_cent", v.valor_mensal_cent), "Mensalidade")
            if setup + mensal <= 0:
                raise ValueError("Informe o valor do setup e/ou da mensalidade.")
            if "servico_id" in d:
                sid = d["servico_id"] or None
                if sid and not db.session.get(Servico, sid):
                    raise ValueError("Serviço inexistente.")
                v.servico_id = sid
            if d.get("fechada_em"):
                v.fechada_em = _data(d["fechada_em"])
            if u.is_admin:
                if d.get("pct_setup") not in (None, ""):
                    v.pct_setup = _pct(d["pct_setup"])
                if d.get("pct_mensal") not in (None, ""):
                    v.pct_mensal = _pct(d["pct_mensal"])
            pct_s = v.pct_setup if u.is_admin else None
            pct_m = v.pct_mensal if u.is_admin else None

            # setup
            pag = next((p for p in v.pagamentos if p.tipo == "setup"), None)
            if pag:
                if setup == 0:
                    if pag.valor_recebido_cent > 0:
                        raise ValueError("O setup já tem valor recebido e não pode ser zerado.")
                    db.session.delete(pag)
                else:
                    if setup < pag.valor_recebido_cent:
                        raise ValueError(f"O setup não pode ficar abaixo do já recebido ({_brl(pag.valor_recebido_cent)}).")
                    pag.valor_cent = setup
                    if d.get("setup_vence"):
                        pag.vence_em = _data(d["setup_vence"])
                    _ajustar_comissao(pag, pct_s)
            elif setup > 0:
                _novo_pagamento(v, "setup", "setup", setup, _data(d.get("setup_vence")) or v.fechada_em)
            v.valor_setup_cent = setup

            # mensalidade
            novo_inicio = _data(d.get("mensal_inicio")) if d.get("mensal_inicio") else None
            livres = [p for p in v.pagamentos if p.tipo == "mensalidade" and p.valor_recebido_cent == 0]
            if mensal == 0:
                for p in livres:
                    db.session.delete(p)
                v.mensal_ativa, v.valor_mensal_cent = False, 0
            else:
                reiniciar = (not v.mensal_ativa) or bool(novo_inicio and novo_inicio != v.mensal_inicio)
                v.valor_mensal_cent, v.mensal_ativa = mensal, True
                if novo_inicio:
                    v.mensal_inicio = novo_inicio
                elif not v.mensal_inicio:
                    v.mensal_inicio = _add_meses(v.fechada_em, 1)
                if reiniciar:
                    for p in livres:
                        db.session.delete(p)
                    db.session.flush()
                    manter_mensalidades(v)
                else:
                    for p in livres:
                        p.valor_cent = mensal
                        _ajustar_comissao(p, pct_m)
            v.versao += 1
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        return jsonify(ok=True, venda_id=v.id)

    @app.delete("/api/admin/vendas/<int:vid>")
    @admin_obrigatorio
    def admin_venda_excluir(vid):
        """Só admin. Apaga venda + pagamentos + comissões. Com dinheiro já recebido exige forcar=true."""
        v, d = db.session.get(Venda, vid), _json()
        if not v:
            abort(404)
        if sum(p.valor_recebido_cent for p in v.pagamentos) > 0 and not d.get("forcar"):
            return _erro("Esta venda já tem pagamentos recebidos. Confirme para excluir mesmo assim.", 409)
        lead = db.session.get(Lead, v.lead_id) if v.lead_id else None
        if lead and lead.status == "fechado":
            lead.status = "negociacao"
            lead.versao += 1
            db.session.add(LeadHistorico(lead_id=lead.id, user_id=usuario_atual().id, tipo="status",
                                         texto="Venda excluída; lead reaberto", status_de="fechado",
                                         status_para="negociacao"))
        db.session.delete(v)
        db.session.commit()
        return jsonify(ok=True)

    @app.get("/api/admin/pagamentos")
    @admin_obrigatorio
    def admin_pagamentos():
        filtro, mes = request.args.get("status", ""), request.args.get("mes", "")
        stmt = (select(Pagamento, Cliente.empresa, User.nome).join(Venda, Venda.id == Pagamento.venda_id)
                .join(Cliente, Cliente.id == Venda.cliente_id).join(User, User.id == Venda.vendedor_id))
        if re.match(r"^\d{4}-(0[1-9]|1[0-2])$", mes):
            y, m = int(mes[:4]), int(mes[5:])
            stmt = stmt.where(Pagamento.vence_em >= date(y, m, 1),
                              Pagamento.vence_em <= date(y, m, calendar.monthrange(y, m)[1]))
        rows = db.session.execute(stmt.order_by(Pagamento.vence_em).limit(500)).all()
        out = [dict(id=p.id, venda_id=p.venda_id, cliente=emp, vendedor=nome, tipo=p.tipo, competencia=p.competencia,
                    valor_cent=p.valor_cent, recebido_cent=p.valor_recebido_cent, vence_em=_fmt(p.vence_em),
                    pago_em=_fmt(p.pago_em), status=p.status) for p, emp, nome in rows]
        if filtro in ("pago", "parcial", "atrasado", "pendente"):
            out = [x for x in out if x["status"] == filtro]
        return jsonify(ok=True, pagamentos=out)

    # ── clientes / projetos ────────────────────────────────────────────────
    def _cli_json(c):
        return dict(id=c.id, owner_id=c.owner_id, empresa=c.empresa, contato=c.contato, documento=c.documento,
                    telefone=c.telefone, email=c.email, site=c.site, endereco=c.endereco, origem=c.origem,
                    observacoes=c.observacoes)

    def _cliente_meu(cid):
        c, u = db.session.get(Cliente, cid), usuario_atual()
        if not c or c.conta_id is not None or (c.owner_id != u.id and not u.is_admin):
            abort(404)
        return c

    def _preencher_cliente(c, d, novo=False):
        c.empresa = _txt(d, "empresa", 160, novo or "empresa" in d, "Empresa") or c.empresa
        for campo, n in (("contato", 120), ("documento", 20), ("telefone", 40), ("site", 300), ("endereco", 300)):
            if campo in d:
                setattr(c, campo, _txt(d, campo, n))
        if "email" in d:
            e = _txt(d, "email", 254)
            if e and not EMAIL_RE.match(e):
                raise ValueError("E-mail inválido.")
            c.email = e
        if "observacoes" in d:
            c.observacoes = _txt(d, "observacoes", 4000) or ""

    def _proj_json(p):
        horas = db.session.scalar(select(func.coalesce(func.sum(ProjetoHoras.horas), 0)).where(ProjetoHoras.projeto_id == p.id))
        return dict(id=p.id, cliente_id=p.cliente_id, nome=p.nome, status=p.status, prazo=_fmt(p.prazo),
                    custo_hora_cent=p.custo_hora_cent, horas=float(horas), custo_mao_obra_cent=round(float(horas) * p.custo_hora_cent),
                    observacoes=p.observacoes)

    def _proj_meu(pid):
        p = db.session.get(Projeto, pid)
        if not p:
            abort(404)
        _cliente_meu(p.cliente_id)
        return p

    @app.get("/api/clientes")
    @login_obrigatorio
    def api_clientes():
        stmt = escopo(select(Cliente), Cliente.owner_id, equipe=request.args.get("equipe") == "1").where(Cliente.conta_id.is_(None))
        return jsonify(ok=True, clientes=[_cli_json(c) for c in db.session.scalars(stmt.order_by(Cliente.empresa).limit(300))])

    @app.post("/api/clientes")
    @login_obrigatorio
    def api_cliente_criar():
        c = Cliente(owner_id=usuario_atual().id, empresa="", origem="manual")
        try:
            _preencher_cliente(c, _json(), novo=True)
        except ValueError as e:
            return _erro(str(e))
        db.session.add(c)
        db.session.commit()
        return jsonify(ok=True, cliente=_cli_json(c))

    @app.get("/api/clientes/<int:cid>")
    @login_obrigatorio
    def api_cliente_ver(cid):
        c = _cliente_meu(cid)
        projetos = db.session.scalars(select(Projeto).where(Projeto.cliente_id == c.id).order_by(Projeto.id.desc()))
        vendas = db.session.scalars(select(Venda).where(Venda.cliente_id == c.id).order_by(Venda.id.desc()))
        return jsonify(ok=True, cliente=_cli_json(c),
                       projetos=[_proj_json(p) for p in projetos],
                       vendas=[dict(id=v.id, setup_cent=v.valor_setup_cent, mensal_cent=v.valor_mensal_cent,
                                    mensal_ativa=v.mensal_ativa, fechada_em=_fmt(v.fechada_em)) for v in vendas])

    @app.put("/api/clientes/<int:cid>")
    @login_obrigatorio
    def api_cliente_editar(cid):
        c = _cliente_meu(cid)
        try:
            _preencher_cliente(c, _json())
        except ValueError as e:
            return _erro(str(e))
        db.session.commit()
        return jsonify(ok=True, cliente=_cli_json(c))

    @app.post("/api/clientes/<int:cid>/venda")
    @login_obrigatorio
    def api_cliente_venda(cid):
        c, d = _cliente_meu(cid), _json()
        try:
            setup, mensal = _centavos(d.get("setup_cent", 0), "Setup"), _centavos(d.get("mensal_cent", 0), "Mensalidade")
            if setup + mensal <= 0:
                raise ValueError("Informe o valor do setup e/ou da mensalidade.")
            sid = d.get("servico_id")
            if sid and not db.session.get(Servico, sid):
                raise ValueError("Serviço inexistente.")
            v = criar_venda(c, db.session.get(User, c.owner_id), setup, mensal, sid,
                            setup_vence=_data(d.get("setup_vence")), mensal_inicio=_data(d.get("mensal_inicio")))
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        return jsonify(ok=True, venda_id=v.id)

    @app.get("/api/projetos")
    @login_obrigatorio
    def api_projetos():
        u = usuario_atual()
        stmt = select(Projeto, Cliente.empresa).join(Cliente, Cliente.id == Projeto.cliente_id)
        if not (u.is_admin and request.args.get("equipe") == "1"):
            stmt = stmt.where(Cliente.owner_id == u.id)
        rows = db.session.execute(stmt.order_by(Projeto.id.desc()).limit(200)).all()
        return jsonify(ok=True, projetos=[dict(_proj_json(p), cliente=emp) for p, emp in rows])

    @app.put("/api/projetos/<int:pid>")
    @login_obrigatorio
    def api_projeto_editar(pid):
        p, d = _proj_meu(pid), _json()
        try:
            if "nome" in d:
                p.nome = _txt(d, "nome", 160, True, "Nome")
            if d.get("status") in ("a_iniciar", "andamento", "entregue"):
                p.status = d["status"]
            if "prazo" in d:
                p.prazo = _data(d["prazo"])
            if "observacoes" in d:
                p.observacoes = _txt(d, "observacoes", 4000) or ""
            if "custo_hora_cent" in d:
                p.custo_hora_cent = _centavos(d["custo_hora_cent"], "Custo/hora")
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        db.session.commit()
        return jsonify(ok=True, projeto=_proj_json(p))

    @app.post("/api/projetos/<int:pid>/horas")
    @login_obrigatorio
    def api_projeto_horas(pid):
        p, d = _proj_meu(pid), _json()
        h = d.get("horas")
        if isinstance(h, bool) or not isinstance(h, (int, float)) or not 0 < h <= 24:
            return _erro("Horas deve ser um número entre 0 e 24.")
        try:
            db.session.add(ProjetoHoras(projeto_id=p.id, user_id=usuario_atual().id, horas=float(h),
                                        data=_data(d.get("data")) or hoje(), descricao=_txt(d, "descricao", 200)))
        except ValueError as e:
            return _erro(str(e))
        db.session.commit()
        return jsonify(ok=True, projeto=_proj_json(p))

    # ── custos e financeiro (admin) ────────────────────────────────────────
    def _custo_json(c):
        return dict(id=c.id, tipo=c.tipo, categoria=c.categoria, descricao=c.descricao, valor_cent=c.valor_cent,
                    recorrencia=c.recorrencia, data=_fmt(c.data), projeto_id=c.projeto_id)

    @app.get("/api/admin/custos")
    @admin_obrigatorio
    def admin_custos():
        rows = db.session.scalars(select(Custo).order_by(Custo.data.desc()).limit(300))
        return jsonify(ok=True, custos=[_custo_json(c) for c in rows])

    @app.post("/api/admin/custos")
    @admin_obrigatorio
    def admin_custo_criar():
        d = _json()
        try:
            if d.get("tipo") not in ("fixo", "variavel"):
                raise ValueError("Tipo deve ser fixo ou variável.")
            rec = d.get("recorrencia", "unica")
            if rec not in ("mensal", "anual", "unica"):
                raise ValueError("Recorrência inválida.")
            pid = d.get("projeto_id")
            if pid and not db.session.get(Projeto, pid):
                raise ValueError("Projeto inexistente.")
            c = Custo(tipo=d["tipo"], categoria=_txt(d, "categoria", 60, True, "Categoria"), descricao=_txt(d, "descricao", 200) or "",
                      valor_cent=_centavos(d.get("valor_cent"), "Valor"), recorrencia=rec, data=_data(d.get("data")) or hoje(),
                      projeto_id=pid or None)
            db.session.add(c)
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        return jsonify(ok=True, custo=_custo_json(c))

    @app.delete("/api/admin/custos/<int:cid>")
    @admin_obrigatorio
    def admin_custo_apagar(cid):
        c = db.session.get(Custo, cid)
        if not c:
            abort(404)
        db.session.delete(c)
        db.session.commit()
        return jsonify(ok=True)

    @app.get("/api/admin/financeiro")
    @admin_obrigatorio
    def admin_financeiro():
        mes = request.args.get("mes") or hoje().strftime("%Y-%m")
        if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", mes):
            return _erro("Mês inválido (use AAAA-MM).")
        y, m = int(mes[:4]), int(mes[5:])
        ini, fim = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
        pags = list(db.session.scalars(select(Pagamento)))
        recebido = sum(p.valor_recebido_cent for p in pags if p.pago_em and ini <= p.pago_em <= fim)
        do_mes = [p for p in pags if ini <= p.vence_em <= fim]
        a_receber = sum(max(p.valor_cent - p.valor_recebido_cent, 0) for p in do_mes)
        atrasado = sum(p.valor_cent - p.valor_recebido_cent for p in pags if p.status == "atrasado")
        custos = 0
        for c in db.session.scalars(select(Custo).where(Custo.data <= fim)):
            if c.recorrencia == "mensal" or (c.recorrencia == "unica" and ini <= c.data <= fim):
                custos += c.valor_cent
            elif c.recorrencia == "anual":
                custos += round(c.valor_cent / 12)
        comissoes = db.session.scalar(select(func.coalesce(func.sum(Comissao.valor_liberado_cent), 0))
                                      .join(Pagamento, Pagamento.id == Comissao.pagamento_id)
                                      .where(Pagamento.pago_em >= ini, Pagamento.pago_em <= fim)) or 0
        mrr = db.session.scalar(select(func.coalesce(func.sum(Venda.valor_mensal_cent), 0)).where(Venda.mensal_ativa.is_(True))) or 0
        return jsonify(ok=True, mes=mes, recebido_cent=recebido, a_receber_cent=a_receber, atrasado_cent=atrasado,
                       custos_cent=custos, comissoes_cent=comissoes, mrr_cent=mrr,
                       lucro_cent=recebido - custos - comissoes)

    # ── candidatos (admin) ─────────────────────────────────────────────────
    ETAPAS = ("novo", "entrevista", "teste", "aprovado", "reprovado")

    def _cand_json(c):
        return dict(id=c.id, nome=c.nome, email=c.email, telefone=c.telefone, etapa=c.etapa, notas=c.notas, user_id=c.user_id)

    @app.get("/api/admin/candidatos")
    @admin_obrigatorio
    def admin_candidatos():
        return jsonify(ok=True, candidatos=[_cand_json(c) for c in db.session.scalars(select(Candidato).order_by(Candidato.id.desc()))])

    @app.route("/api/admin/candidatos", methods=["POST"])
    @app.route("/api/admin/candidatos/<int:cid>", methods=["PUT"])
    @admin_obrigatorio
    def admin_candidato(cid=None):
        d = _json()
        c = db.session.get(Candidato, cid) if cid else Candidato()
        if cid and not c:
            abort(404)
        try:
            if not cid or "nome" in d:
                c.nome = _txt(d, "nome", 120, True, "Nome")
            if "email" in d:
                e = _txt(d, "email", 254)
                if e and not EMAIL_RE.match(e):
                    raise ValueError("E-mail inválido.")
                c.email = e
            if "telefone" in d:
                c.telefone = _txt(d, "telefone", 40)
            if "notas" in d:
                c.notas = _txt(d, "notas", 4000) or ""
            if d.get("etapa") in ETAPAS:
                c.etapa = d["etapa"]
            db.session.add(c)
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        return jsonify(ok=True, candidato=_cand_json(c))

    @app.post("/api/admin/candidatos/<int:cid>/aprovar")
    @admin_obrigatorio
    def admin_candidato_aprovar(cid):
        c = db.session.get(Candidato, cid)
        if not c:
            abort(404)
        if c.user_id:
            return _erro("Esse candidato já tem acesso.", 409)
        email = _norm(c.email)
        if not EMAIL_RE.match(email):
            return _erro("Cadastre um e-mail válido no candidato antes de aprovar.")
        temp = secrets.token_urlsafe(9)
        try:
            u = User(nome=c.nome[:80], email=email, email_norm=email, telefone=c.telefone, papel="vendedor",
                     senha_hash=generate_password_hash(temp), trocar_senha=True)
            db.session.add(u)
            db.session.flush()
            c.user_id, c.etapa = u.id, "aprovado"
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return _erro("Já existe um usuário com esse e-mail.", 409)
        return jsonify(ok=True, usuario=_user_json(u), senha_temporaria=temp)

    # ── treinamento ────────────────────────────────────────────────────────
    @app.get("/api/treinamento")
    @login_obrigatorio
    def api_treinamento():
        u = usuario_atual()
        feitos = set(db.session.scalars(select(TreinamentoProgresso.modulo_id).where(TreinamentoProgresso.user_id == u.id)))
        mods = db.session.scalars(select(TreinamentoModulo).order_by(TreinamentoModulo.ordem, TreinamentoModulo.id))
        return jsonify(ok=True, modulos=[dict(id=t.id, titulo=t.titulo, ordem=t.ordem, conteudo=t.conteudo,
                                              concluido=t.id in feitos) for t in mods])

    @app.post("/api/treinamento/<int:mid>/concluir")
    @login_obrigatorio
    def api_treinamento_concluir(mid):
        u = usuario_atual()
        if not db.session.get(TreinamentoModulo, mid):
            abort(404)
        if not db.session.get(TreinamentoProgresso, (u.id, mid)):
            db.session.add(TreinamentoProgresso(user_id=u.id, modulo_id=mid))
            db.session.commit()
        return jsonify(ok=True)

    @app.route("/api/admin/treinamento", methods=["POST"])
    @app.route("/api/admin/treinamento/<int:mid>", methods=["PUT"])
    @admin_obrigatorio
    def admin_treinamento(mid=None):
        d = _json()
        t = db.session.get(TreinamentoModulo, mid) if mid else TreinamentoModulo()
        if mid and not t:
            abort(404)
        try:
            if not mid or "titulo" in d:
                t.titulo = _txt(d, "titulo", 160, True, "Título")
            if "conteudo" in d:
                t.conteudo = _txt(d, "conteudo", 20000) or ""
            if "ordem" in d:
                if isinstance(d["ordem"], bool) or not isinstance(d["ordem"], int):
                    raise ValueError("Ordem inválida.")
                t.ordem = d["ordem"]
            db.session.add(t)
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        return jsonify(ok=True, id=t.id)

    # ── materiais ──────────────────────────────────────────────────────────
    @app.get("/api/materiais")
    @login_obrigatorio
    def api_materiais():
        rows = db.session.scalars(select(Material).order_by(Material.categoria, Material.titulo))
        return jsonify(ok=True, materiais=[dict(id=x.id, titulo=x.titulo, categoria=x.categoria, link=x.link,
                                                tem_arquivo=bool(x.arquivo)) for x in rows])

    @app.post("/api/admin/materiais")
    @admin_obrigatorio
    def admin_material_criar():
        f = request.form if request.form else _json()
        arq, nome_arq = request.files.get("arquivo"), None
        try:
            titulo = _txt(f, "titulo", 160, True, "Título")
            link = _txt(f, "link", 500)
            if link and not link.lower().startswith(("http://", "https://")):
                raise ValueError("O link precisa começar com http:// ou https://.")
            if arq and arq.filename:
                nome = secrets.token_hex(6) + "_" + secure_filename(arq.filename)
                if Path(nome).suffix.lower() not in EXT_PERMITIDAS:
                    raise ValueError("Tipo de arquivo não permitido.")
                (DATA_DIR / "uploads").mkdir(parents=True, exist_ok=True)
                arq.save(DATA_DIR / "uploads" / nome)
                nome_arq = nome
            if not link and not nome_arq:
                raise ValueError("Envie um arquivo ou informe um link.")
        except ValueError as e:
            return _erro(str(e))
        x = Material(titulo=titulo, categoria=_txt(f, "categoria", 60) or "geral", arquivo=nome_arq, link=link)
        db.session.add(x)
        db.session.commit()
        return jsonify(ok=True, id=x.id)

    @app.delete("/api/admin/materiais/<int:mid>")
    @admin_obrigatorio
    def admin_material_apagar(mid):
        x = db.session.get(Material, mid)
        if not x:
            abort(404)
        if x.arquivo:
            (DATA_DIR / "uploads" / x.arquivo).unlink(missing_ok=True)
        db.session.delete(x)
        db.session.commit()
        return jsonify(ok=True)

    @app.get("/api/materiais/<int:mid>/arquivo")
    @login_obrigatorio
    def api_material_arquivo(mid):
        x = db.session.get(Material, mid)
        if not x or not x.arquivo:
            abort(404)
        return send_from_directory(DATA_DIR / "uploads", x.arquivo, as_attachment=True)

    # ═══════════════════════════════════════════════════════════════════════
    # ADMIN: contas (clientes seus) e acessos delas. Só o administrador da plataforma (quem não tem conta).
    # ═══════════════════════════════════════════════════════════════════════
    def _conta_json(c):
        us = db.session.scalars(select(User).where(User.conta_id == c.id).order_by(User.nome))
        return dict(id=c.id, nome=c.nome, nicho=c.nicho, nicho_ok=c.nicho in NICHOS, ativo=c.ativo,
                    usuarios=[_user_json(x) for x in us], criado_em=_fmt(c.criado_em))

    def _nicho_valido_p_conta(nid):
        m = NICHOS.get(nid)
        if m is None:
            raise ValueError("Esse nicho não existe ou está com erro no nicho.json.")
        if not nicho_serve_conta(m):
            raise ValueError("Esse nicho usa módulos de agência e não pode ser vinculado a uma conta.")
        return nid

    @app.get("/admin/contas")
    @login_obrigatorio
    def pagina_contas():
        if not usuario_atual().is_admin:
            return redirect("/app")
        return render_template("contas.html")

    @app.get("/api/admin/contas")
    @admin_obrigatorio
    def admin_contas():
        contas = db.session.scalars(select(Conta).order_by(Conta.nome))
        return jsonify(ok=True, contas=[_conta_json(c) for c in contas],
                       nichos=[dict(id=k, nome=v["nome"], cor=v["cor"]) for k, v in NICHOS.items() if nicho_serve_conta(v)],
                       nichos_com_erro=NICHOS_ERRO)

    @app.post("/api/admin/contas")
    @admin_obrigatorio
    def admin_conta_criar():
        d = _json()
        try:
            c = Conta(nome=_txt(d, "nome", 120, True, "Nome do negócio"), nicho=_nicho_valido_p_conta(d.get("nicho")))
        except ValueError as e:
            return _erro(str(e))
        db.session.add(c)
        db.session.commit()
        return jsonify(ok=True, conta=_conta_json(c))

    @app.put("/api/admin/contas/<int:cid>")
    @admin_obrigatorio
    def admin_conta_editar(cid):
        c, d = db.session.get(Conta, cid), _json()
        if not c:
            abort(404)
        try:
            if "nome" in d:
                c.nome = _txt(d, "nome", 120, True, "Nome do negócio")
            if "ativo" in d:
                c.ativo = bool(d["ativo"])
            if "nicho" in d and d["nicho"] != c.nicho:
                tem_dados = db.session.scalar(select(func.count()).select_from(Cliente).where(Cliente.conta_id == c.id))
                if tem_dados:
                    raise ValueError("Esta conta já tem dados. Trocar o nicho deixaria registros sem painel. Crie uma conta nova.")
                c.nicho = _nicho_valido_p_conta(d["nicho"])
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        db.session.commit()
        return jsonify(ok=True, conta=_conta_json(c))

    @app.post("/api/admin/contas/<int:cid>/usuarios")
    @admin_obrigatorio
    def admin_conta_usuario(cid):
        c, d = db.session.get(Conta, cid), _json()
        if not c:
            abort(404)
        nome, email = (d.get("nome") or "").strip()[:80], _norm(d.get("email"))
        if not nome or not EMAIL_RE.match(email):
            return _erro("Informe nome e e-mail válido.")
        temp = secrets.token_urlsafe(9)
        try:
            u = User(nome=nome, email=email, email_norm=email, papel="vendedor", conta_id=c.id,
                     telefone=(d.get("telefone") or "")[:30] or None,
                     senha_hash=generate_password_hash(temp), trocar_senha=True)
            db.session.add(u)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return _erro("Já existe um usuário com esse e-mail.", 409)
        return jsonify(ok=True, usuario=_user_json(u), senha_temporaria=temp)      # só aparece desta vez

    # ═══════════════════════════════════════════════════════════════════════
    # NICHOS: API genérica (clientes + registros guiados pelo manifesto da CONTA de quem está logado)
    #   Tudo é filtrado pela conta: um cliente seu nunca enxerga dados de outro.
    #   Só existe para quem tem conta (para os demais /api/n/* responde 404).
    # ═══════════════════════════════════════════════════════════════════════
    def _M():
        return manifesto_do(usuario_atual())

    def _tipos():
        return {t["id"]: t for t in _M()["tipos_registro"]}

    def _n_equipe():
        return _M()["visibilidade"] == "equipe"

    def _n_visivel(stmt, modelo):
        """modelo = Cliente ou Registro. Sempre dentro da conta; se a visibilidade for 'proprio', só o que a pessoa criou."""
        u = usuario_atual()
        stmt = stmt.where(modelo.conta_id == u.conta_id)
        return stmt if _n_equipe() else stmt.where(modelo.owner_id == u.id)

    def _n_pode(obj):
        u = usuario_atual()
        return obj.conta_id == u.conta_id and (_n_equipe() or obj.owner_id == u.id)

    def _n_ids_users():
        return select(User.id).where(User.conta_id == usuario_atual().conta_id)

    def _n_cliente(cid):
        c = db.session.get(Cliente, cid)
        if not c or not _n_pode(c):
            abort(404)
        return c

    def _n_registro(rid):
        r = db.session.get(Registro, rid)
        if not r or not _n_pode(r):
            abort(404)
        return r

    def _n_cli_json(c):
        return dict(id=c.id, nome=c.empresa, telefone=c.telefone, email=c.email, documento=c.documento,
                    endereco=c.endereco, observacoes=c.observacoes, dados=c.dados or {}, criado_em=_fmt(c.criado_em))

    def _n_pg_json(p):
        return dict(id=p.id, registro_id=p.registro_id, descricao=p.descricao, valor_cent=p.valor_cent,
                    recebido_cent=p.valor_recebido_cent, vence_em=_fmt(p.vence_em), pago_em=_fmt(p.pago_em), status=p.status,
                    forma=p.forma, parcelas=p.parcelas, taxa_cent=p.taxa_cent or 0)

    def _n_custo_json(x):
        return dict(id=x.id, cliente_id=x.cliente_id, registro_id=x.registro_id, categoria=x.categoria,
                    descricao=x.descricao, valor_cent=x.valor_cent, data=_fmt(x.data))

    def _n_pmsg_json(m):
        return dict(id=m.id, de=m.de, tipo=m.tipo, texto=m.texto, nome=m.nome, tamanho=m.tamanho,
                    tem_arquivo=bool(m.arquivo), nova=(m.de == "cliente" and not m.lida), em=_fmt(m.criado_em))

    def _n_portal_json(c):
        if not c.portal_hash:
            return dict(ativo=False)
        tok = decifrar(c.portal_token_enc)
        if not tok:
            return dict(ativo=False)
        url = request.host_url.rstrip("/") + "/c/" + tok
        loja = (db.session.get(Conta, c.conta_id).nome if c.conta_id else "")
        wa = wa_link(c.telefone, f"Oi {(c.empresa or '').split(' ')[0]}! Este é o seu acesso pessoal à área do cliente da {loja}: {url}")
        return dict(ativo=True, url=url, whatsapp=wa, aceite_em=_fmt(c.portal_aceite_em), ultimo_acesso=_fmt(c.portal_ultimo_acesso))

    def _n_anexo_json(a):
        return dict(id=a.id, nome=a.nome, mime=a.mime, tamanho=a.tamanho, em=_fmt(a.criado_em))

    def _n_reg_json(r, cliente_nome=None):
        return dict(id=r.id, tipo=r.tipo, cliente_id=r.cliente_id, cliente_nome=cliente_nome, titulo=r.titulo,
                    status=r.status, data_ref=_fmt(r.data_ref), valor_cent=r.valor_cent, dados=r.dados or {},
                    versao=r.versao, criado_em=_fmt(r.criado_em), atualizado_em=_fmt(r.atualizado_em))

    def _n_ativ_json(a, cliente_nome=None):
        return dict(id=a.id, titulo=a.titulo, vence_em=_fmt(a.vence_em), feita_em=_fmt(a.feita_em),
                    atrasada=bool(a.vence_em and not a.feita_em and a.vence_em < agora_local()),
                    cliente_id=a.cliente_id, cliente_nome=cliente_nome, registro_id=a.registro_id)

    def _n_preencher_cliente(c, d, novo=False):
        campos = _M()["campos_cliente"]
        c.empresa = _txt(d, "nome", 160, novo or "nome" in d, "Nome") or c.empresa
        for campo, n in (("telefone", 40), ("documento", 20), ("endereco", 300)):
            if campo in d:
                setattr(c, campo, _txt(d, campo, n))
        if "email" in d:
            e = _txt(d, "email", 254)
            if e and not EMAIL_RE.match(e):
                raise ValueError("E-mail inválido.")
            c.email = e
        if "observacoes" in d:
            c.observacoes = _txt(d, "observacoes", 4000) or ""
        if novo or isinstance(d.get("dados"), dict):
            entrada = d.get("dados") if isinstance(d.get("dados"), dict) else {}
            if novo:
                entrada = {**padroes(campos), **entrada}
            c.dados = validar_dados(campos, entrada, c.dados)

    @app.get("/api/n/schema")
    @login_obrigatorio
    def n_schema():
        u = usuario_atual()
        return jsonify(ok=True, nicho=_M(), app_nome=nome_app_do(u),
                       usuario=dict(id=u.id, nome=u.nome, admin=u.is_admin))

    # ── clientes ───────────────────────────────────────────────────────────
    @app.get("/api/n/clientes")
    @login_obrigatorio
    def n_clientes():
        stmt = _n_visivel(select(Cliente), Cliente)
        q = (request.args.get("q") or "").strip()[:80]
        if q:
            stmt = stmt.where(or_(Cliente.empresa.icontains(q, autoescape=True),
                                  Cliente.telefone.icontains(q, autoescape=True),
                                  Cliente.email.icontains(q, autoescape=True)))
        rows = db.session.scalars(stmt.order_by(Cliente.empresa).limit(300))
        return jsonify(ok=True, clientes=[_n_cli_json(c) for c in rows])

    @app.post("/api/n/clientes")
    @login_obrigatorio
    def n_cliente_criar():
        u = usuario_atual()
        c = Cliente(owner_id=u.id, conta_id=u.conta_id, empresa="", origem="manual")
        try:
            _n_preencher_cliente(c, _json(), novo=True)
        except ValueError as e:
            return _erro(str(e))
        db.session.add(c)
        db.session.commit()
        return jsonify(ok=True, cliente=_n_cli_json(c))

    @app.get("/api/n/clientes/<int:cid>")
    @login_obrigatorio
    def n_cliente_ver(cid):
        c = _n_cliente(cid)
        regs = db.session.scalars(select(Registro).where(Registro.cliente_id == c.id)
                                  .order_by(Registro.data_ref.desc(), Registro.id.desc()).limit(300))
        a_receber = db.session.scalar(
            select(func.coalesce(func.sum(RegistroPagamento.valor_cent - RegistroPagamento.valor_recebido_cent), 0))
            .select_from(RegistroPagamento).join(Registro, Registro.id == RegistroPagamento.registro_id)
            .where(Registro.cliente_id == c.id, RegistroPagamento.valor_recebido_cent < RegistroPagamento.valor_cent))
        recebido, taxas = db.session.execute(
            select(func.coalesce(func.sum(RegistroPagamento.valor_recebido_cent), 0),
                   func.coalesce(func.sum(RegistroPagamento.taxa_cent), 0))
            .select_from(RegistroPagamento).join(Registro, Registro.id == RegistroPagamento.registro_id)
            .where(Registro.cliente_id == c.id)).one()
        custos = list(db.session.scalars(select(CustoCliente).where(CustoCliente.cliente_id == c.id)
                                         .order_by(CustoCliente.data.desc(), CustoCliente.id.desc()).limit(300)))
        total_custos = sum(x.valor_cent for x in custos)
        msgs = list(db.session.scalars(select(PortalMsg).where(PortalMsg.cliente_id == c.id)
                                       .order_by(PortalMsg.id.desc()).limit(60)))[::-1]
        return jsonify(ok=True, cliente=_n_cli_json(c), registros=[_n_reg_json(r, c.empresa) for r in regs],
                       portal=_n_portal_json(c), mensagens=[_n_pmsg_json(x) for x in msgs],
                       nao_lidas=sum(1 for x in msgs if x.de == "cliente" and not x.lida),
                       a_receber_cent=int(a_receber), custos=[_n_custo_json(x) for x in custos],
                       lucro=dict(recebido_cent=int(recebido), taxas_cent=int(taxas), custos_cent=total_custos,
                                  liquido_cent=int(recebido) - int(taxas) - total_custos))

    @app.put("/api/n/clientes/<int:cid>")
    @login_obrigatorio
    def n_cliente_editar(cid):
        c = _n_cliente(cid)
        try:
            _n_preencher_cliente(c, _json())
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        db.session.commit()
        return jsonify(ok=True, cliente=_n_cli_json(c))

    @app.delete("/api/n/clientes/<int:cid>")
    @login_obrigatorio
    def n_cliente_apagar(cid):
        c = _n_cliente(cid)
        if db.session.scalar(select(func.count()).select_from(Registro).where(Registro.cliente_id == c.id)):
            return _erro("Este cadastro tem registros. Apague-os primeiro.", 409)
        arquivos = [m.arquivo for m in db.session.scalars(select(PortalMsg).where(PortalMsg.cliente_id == c.id)) if m.arquivo]
        db.session.delete(c)
        db.session.commit()
        for nome in arquivos:
            (DATA_DIR / "uploads" / nome).unlink(missing_ok=True)
        return jsonify(ok=True)

    # ── registros ──────────────────────────────────────────────────────────
    @app.get("/api/n/registros")
    @login_obrigatorio
    def n_registros():
        a = request.args
        stmt = _n_visivel(select(Registro, Cliente.empresa).join(Cliente, Cliente.id == Registro.cliente_id), Registro)
        try:
            if a.get("tipo"):
                stmt = stmt.where(Registro.tipo == a["tipo"])
            if a.get("status"):
                stmt = stmt.where(Registro.status == a["status"])
            if a.get("cliente_id"):
                stmt = stmt.where(Registro.cliente_id == int(a["cliente_id"]))
            if a.get("de"):
                stmt = stmt.where(Registro.data_ref >= _data(a["de"]))
            if a.get("ate"):
                stmt = stmt.where(Registro.data_ref <= _data(a["ate"]))
        except ValueError:
            return _erro("Filtro inválido.")
        if a.get("abertos") == "1":
            stmt = stmt.where(or_(*[and_(Registro.tipo == t["id"], Registro.status.notin_(t["etapas_finais"]))
                                    for t in _M()["tipos_registro"]]))
        ordem = Registro.data_ref.asc() if a.get("ordem") == "asc" else Registro.data_ref.desc()
        rows = db.session.execute(stmt.order_by(ordem, Registro.id.desc()).limit(500))
        return jsonify(ok=True, registros=[_n_reg_json(r, nome) for r, nome in rows])

    @app.post("/api/n/registros")
    @login_obrigatorio
    def n_registro_criar():
        d, u = _json(), usuario_atual()
        try:
            t = _tipos().get(d.get("tipo"))
            if not t:
                raise ValueError("Tipo de registro inválido.")
            c = _n_cliente(_int_id(d.get("cliente_id"), "Cadastro"))
            status = d.get("status") or t["etapas"][0]
            if status not in t["etapas"]:
                raise ValueError("Etapa inválida.")
            r = Registro(owner_id=u.id, conta_id=u.conta_id, cliente_id=c.id, tipo=t["id"], status=status)
            entrada = d.get("dados") if isinstance(d.get("dados"), dict) else {}
            r.dados = validar_dados(t["campos"], {**padroes(t["campos"]), **entrada}, None)
            derivar_colunas(r, t)
            db.session.add(r)
            db.session.flush()
            db.session.add(RegistroHistorico(registro_id=r.id, user_id=u.id, tipo="nota", texto=f"{t['rotulo']} criado"))
            if t["pagamentos"] and t["pagamentos"]["sinal_pct"] and r.valor_cent > 0:
                gerar_sinal(r, t["pagamentos"]["sinal_pct"])
            sincronizar_lembretes(r, t, c)
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        return jsonify(ok=True, registro=_n_reg_json(r, c.empresa))

    @app.get("/api/n/registros/<int:rid>")
    @login_obrigatorio
    def n_registro_ver(rid):
        r = _n_registro(rid)
        hist = db.session.scalars(select(RegistroHistorico).where(RegistroHistorico.registro_id == r.id)
                                  .order_by(RegistroHistorico.id.desc()).limit(50))
        c = db.session.get(Cliente, r.cliente_id)
        anexos = db.session.scalars(select(Anexo).where(Anexo.registro_id == r.id).order_by(Anexo.id))
        return jsonify(ok=True, registro=_n_reg_json(r, c.empresa), cliente=_n_cli_json(c),
                       anexos=[_n_anexo_json(a) for a in anexos],
                       pagamentos=[_n_pg_json(p) for p in r.pagamentos],
                       historico=[dict(tipo=h.tipo, texto=h.texto, de=h.status_de, para=h.status_para,
                                       em=_fmt(h.criado_em)) for h in hist])

    @app.put("/api/n/registros/<int:rid>")
    @login_obrigatorio
    def n_registro_editar(rid):
        r, d, u = _n_registro(rid), _json(), usuario_atual()
        t = _tipos().get(r.tipo)
        if not t:
            return _erro("Este tipo de registro não existe mais no manifesto do nicho.", 409)
        if "versao" in d and d["versao"] != r.versao:
            return _erro("Este registro foi alterado por outra pessoa. Recarregue.", 409)
        try:
            if isinstance(d.get("dados"), dict):
                r.dados = validar_dados(t["campos"], d["dados"], r.dados)
                derivar_colunas(r, t)
            novo = d.get("status")
            if novo and novo != r.status:
                if novo not in t["etapas"]:
                    raise ValueError("Etapa inválida.")
                db.session.add(RegistroHistorico(registro_id=r.id, user_id=u.id, tipo="status", texto="",
                                                 status_de=r.status, status_para=novo))
                r.status = novo
            r.versao += 1
            sincronizar_lembretes(r, t, db.session.get(Cliente, r.cliente_id))
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        return jsonify(ok=True, registro=_n_reg_json(r, db.session.get(Cliente, r.cliente_id).empresa))

    @app.delete("/api/n/registros/<int:rid>")
    @login_obrigatorio
    def n_registro_apagar(rid):
        r = _n_registro(rid)
        arquivos = [a.arquivo for a in db.session.scalars(select(Anexo).where(Anexo.registro_id == r.id))]
        db.session.delete(r)
        db.session.commit()
        for nome in arquivos:
            (DATA_DIR / "uploads" / nome).unlink(missing_ok=True)
        return jsonify(ok=True)

    @app.post("/api/n/registros/<int:rid>/nota")
    @login_obrigatorio
    def n_registro_nota(rid):
        r, d = _n_registro(rid), _json()
        try:
            txt = _txt(d, "texto", 2000, True, "Texto")
        except ValueError as e:
            return _erro(str(e))
        db.session.add(RegistroHistorico(registro_id=r.id, user_id=usuario_atual().id, tipo="nota", texto=txt))
        r.atualizado_em = agora()
        db.session.commit()
        return jsonify(ok=True)


    # ── taxas da maquininha (por conta) ────────────────────────────────────
    @app.get("/api/n/taxas")
    @login_obrigatorio
    def n_taxas():
        return jsonify(ok=True, taxas=taxas_da_conta(conta_do(usuario_atual())))

    @app.put("/api/n/taxas")
    @admin_obrigatorio
    def n_taxas_salvar():
        conta, d = conta_do(usuario_atual()), _json()
        if not conta:
            abort(404)
        novo = taxas_da_conta(conta)
        try:
            for k in novo:
                if k in d and d[k] not in (None, ""):
                    v = float(str(d[k]).replace(",", "."))
                    if not math.isfinite(v) or not 0 <= v <= 100:
                        raise ValueError("Cada taxa deve estar entre 0% e 100%.")
                    novo[k] = round(v, 2)
        except ValueError as e:
            return _erro(str(e) if "taxa" in str(e) else "Taxa inválida.")
        conta.taxas = novo
        db.session.commit()
        return jsonify(ok=True, taxas=novo)

    # ── custos por cliente ─────────────────────────────────────────────────
    @app.post("/api/n/clientes/<int:cid>/custos")
    @login_obrigatorio
    def n_custo_criar(cid):
        c, d, u = _n_cliente(cid), _json(), usuario_atual()
        try:
            valor = _centavos(d.get("valor_cent"), "Valor")
            if valor <= 0:
                raise ValueError("Informe um valor maior que zero.")
            rid = d.get("registro_id")
            if rid not in (None, ""):
                if _n_registro(_int_id(rid, "Registro")).cliente_id != c.id:
                    raise ValueError("Esse registro é de outro cadastro.")
            x = CustoCliente(conta_id=u.conta_id, owner_id=u.id, cliente_id=c.id, registro_id=rid or None,
                             categoria=_txt(d, "categoria", 40) or "Outros", descricao=_txt(d, "descricao", 160) or "",
                             valor_cent=valor, data=_data(d.get("data")) or hoje())
            db.session.add(x)
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        return jsonify(ok=True, custo=_n_custo_json(x))

    @app.delete("/api/n/custos/<int:xid>")
    @login_obrigatorio
    def n_custo_apagar(xid):
        x = db.session.get(CustoCliente, xid)
        if not x or x.conta_id != usuario_atual().conta_id:
            abort(404)
        _n_cliente(x.cliente_id)                    # herda a regra de visibilidade
        db.session.delete(x)
        db.session.commit()
        return jsonify(ok=True)

    # ── anexos do registro (foto/PDF da receita) ───────────────────────────
    @app.post("/api/n/registros/<int:rid>/anexos")
    @login_obrigatorio
    def n_anexo_criar(rid):
        r, u = _n_registro(rid), usuario_atual()
        t = _tipos().get(r.tipo)
        if not t or not t["anexos"]:
            return _erro("Este tipo de registro não aceita arquivos.", 400)
        arq = request.files.get("arquivo")
        if not arq or not arq.filename:
            return _erro("Escolha um arquivo.")
        ext = Path(arq.filename).suffix.lower()
        if ext not in EXT_ANEXO:
            return _erro("Envie PDF, PNG, JPG ou WEBP.")
        if db.session.scalar(select(func.count()).select_from(Anexo).where(Anexo.registro_id == r.id)) >= MAX_ANEXOS_REGISTRO:
            return _erro(f"Limite de {MAX_ANEXOS_REGISTRO} arquivos por registro.")
        cab = arq.stream.read(16)
        arq.stream.seek(0)
        if not _assinatura_ok(ext, cab):
            return _erro("O conteúdo não parece ser um " + ext[1:].upper() + " de verdade.")
        pasta = DATA_DIR / "uploads" / "contas" / str(u.conta_id)
        pasta.mkdir(parents=True, exist_ok=True)
        rel = f"contas/{u.conta_id}/{secrets.token_hex(12)}{ext}"
        arq.save(DATA_DIR / "uploads" / rel)
        a = Anexo(conta_id=u.conta_id, registro_id=r.id, nome=secure_filename(arq.filename)[:200] or f"arquivo{ext}",
                  arquivo=rel, mime=EXT_ANEXO[ext], tamanho=(DATA_DIR / "uploads" / rel).stat().st_size)
        db.session.add(a)
        db.session.add(RegistroHistorico(registro_id=r.id, user_id=u.id, tipo="nota", texto=f"Arquivo anexado: {a.nome}"))
        db.session.commit()
        return jsonify(ok=True, anexo=_n_anexo_json(a))

    def _n_anexo(aid):
        a = db.session.get(Anexo, aid)
        if not a or a.conta_id != usuario_atual().conta_id:
            abort(404)
        _n_registro(a.registro_id)                   # herda a regra de visibilidade
        return a

    @app.get("/api/n/anexos/<int:aid>")
    @login_obrigatorio
    def n_anexo_ver(aid):
        a = _n_anexo(aid)
        resp = send_from_directory(DATA_DIR / "uploads", a.arquivo, mimetype=a.mime, download_name=a.nome)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "private, no-store"
        return resp

    @app.delete("/api/n/anexos/<int:aid>")
    @login_obrigatorio
    def n_anexo_apagar(aid):
        a = _n_anexo(aid)
        (DATA_DIR / "uploads" / a.arquivo).unlink(missing_ok=True)
        db.session.delete(a)
        db.session.commit()
        return jsonify(ok=True)


    # ── área do cliente: o que a LOJA faz (gerar/desativar link, responder) ─
    @app.post("/api/n/clientes/<int:cid>/portal")
    @login_obrigatorio
    def n_portal_gerar(cid):
        c = _n_cliente(cid)
        tok = secrets.token_urlsafe(24)
        c.portal_hash = hashlib.sha256(tok.encode()).hexdigest()
        c.portal_token_enc = cifrar(tok)
        db.session.commit()
        return jsonify(ok=True, portal=_n_portal_json(c))

    @app.delete("/api/n/clientes/<int:cid>/portal")
    @login_obrigatorio
    def n_portal_desativar(cid):
        c = _n_cliente(cid)
        c.portal_hash = c.portal_token_enc = None
        db.session.commit()
        return jsonify(ok=True, portal=_n_portal_json(c))

    @app.post("/api/n/clientes/<int:cid>/mensagens")
    @login_obrigatorio
    def n_pmsg_enviar(cid):
        c, d, u = _n_cliente(cid), _json(), usuario_atual()
        try:
            texto = _txt(d, "texto", 2000, True, "Mensagem")
        except ValueError as e:
            return _erro(str(e))
        m = PortalMsg(conta_id=u.conta_id, cliente_id=c.id, de="loja", tipo="mensagem", texto=texto, lida=True, user_id=u.id)
        db.session.add(m)
        db.session.commit()
        return jsonify(ok=True, mensagem=_n_pmsg_json(m))

    @app.post("/api/n/clientes/<int:cid>/mensagens/lidas")
    @login_obrigatorio
    def n_pmsg_lidas(cid):
        c = _n_cliente(cid)
        db.session.execute(PortalMsg.__table__.update().where(PortalMsg.cliente_id == c.id, PortalMsg.de == "cliente")
                           .values(lida=True))
        db.session.commit()
        return jsonify(ok=True)

    @app.get("/api/n/pmsg/<int:mid>/arquivo")
    @login_obrigatorio
    def n_pmsg_arquivo(mid):
        m = db.session.get(PortalMsg, mid)
        if not m or not m.arquivo or m.conta_id != usuario_atual().conta_id:
            abort(404)
        _n_cliente(m.cliente_id)
        resp = send_from_directory(DATA_DIR / "uploads", m.arquivo, mimetype=m.mime, download_name=m.nome)
        resp.headers["Cache-Control"] = "private, no-store"
        return resp

    # ── área do cliente FINAL: sem login, só o link pessoal (/c/<token>) ────
    def _portal_busca(token):
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", token or "") or not limitar(f"portal:{request.remote_addr}", 300, 600):
            return None
        c = db.session.scalar(select(Cliente).where(Cliente.portal_hash == hashlib.sha256(token.encode()).hexdigest()))
        conta = db.session.get(Conta, c.conta_id) if c and c.conta_id else None
        m = NICHOS.get(conta.nicho) if conta else None
        return (c, conta, m) if conta and conta.ativo and m else None

    def _portal(token, exige_aceite=True):
        r = _portal_busca(token)
        if not r:
            abort(404)
        c = r[0]
        if exige_aceite and c.portal_aceite_versao != TERMO_VERSAO:
            abort(403)
        return r

    @app.get("/c/<token>")
    def portal_pagina(token):
        r = _portal_busca(token)
        return render_template("portal.html", loja=(r[1].nome if r else ""), cor=(r[2]["cor"] if r else "#2563eb"))

    def _portal_visivel(campos):
        return [x for x in campos if not x.get("interno")]

    @app.get("/api/c/<token>")
    def portal_dados_ver(token):
        c, conta, m = _portal(token, exige_aceite=False)
        c.portal_ultimo_acesso = agora()
        db.session.commit()
        base = dict(ok=True, loja=conta.nome, cor=m["cor"], termo=termo_portal(conta.nome), termo_versao=TERMO_VERSAO,
                    aceite=c.portal_aceite_versao == TERMO_VERSAO, primeiro_nome=(c.empresa or "").split(" ")[0])
        if not base["aceite"]:
            return jsonify(base)
        campos_cli = _portal_visivel(m["campos_cliente"])
        grupos = []
        for t in m["tipos_registro"]:
            if not t.get("portal", True):
                continue
            vis = _portal_visivel(t["campos"])
            chaves = {x["chave"] for x in vis}
            regs = db.session.scalars(select(Registro).where(Registro.cliente_id == c.id, Registro.tipo == t["id"])
                                      .order_by(Registro.data_ref.desc(), Registro.id.desc()).limit(50))
            itens = []
            for r in regs:
                anexos = [dict(id=a.id, nome=a.nome, mime=a.mime, tamanho=a.tamanho) for a in
                          db.session.scalars(select(Anexo).where(Anexo.registro_id == r.id).order_by(Anexo.id))] if t["anexos"] else []
                itens.append(dict(id=r.id, titulo=r.titulo, status=r.status, data_ref=_fmt(r.data_ref), valor_cent=r.valor_cent,
                                  dados={k: v for k, v in (r.dados or {}).items() if k in chaves}, anexos=anexos,
                                  pagamentos=[dict(descricao=p.descricao, valor_cent=p.valor_cent, recebido_cent=p.valor_recebido_cent,
                                                   vence_em=_fmt(p.vence_em), status=p.status) for p in r.pagamentos] if t["pagamentos"] else []))
            grupos.append(dict(id=t["id"], rotulo=t["rotulo"], rotulo_plural=t["rotulo_plural"], etapas_finais=t["etapas_finais"],
                               valor=t["valor"], campos=[{k: x[k] for k in ("chave", "rotulo", "tipo", "grupo") if k in x} for x in vis],
                               itens=itens))
        msgs = db.session.scalars(select(PortalMsg).where(PortalMsg.cliente_id == c.id, PortalMsg.tipo != "dados")
                                  .order_by(PortalMsg.id.desc()).limit(100))
        base.update(
            cliente=dict(nome=c.empresa, telefone=c.telefone, email=c.email, endereco=c.endereco, documento=c.documento,
                         dados=c.dados or {}),
            campos_cliente=[{k: x[k] for k in ("chave", "rotulo", "tipo", "opcoes", "portal", "ajuda") if k in x} for x in campos_cli],
            grupos=grupos,
            mensagens=[dict(id=x.id, de=x.de, tipo=x.tipo, texto=x.texto, nome=x.nome, tem_arquivo=bool(x.arquivo),
                            em=_fmt(x.criado_em)) for x in list(msgs)[::-1]])
        return jsonify(base)

    @app.post("/api/c/<token>/aceite")
    def portal_aceitar(token):
        c, _, _ = _portal(token, exige_aceite=False)
        if (_json().get("aceito") is not True):
            return _erro("Marque que leu e concorda para continuar.")
        c.portal_aceite_em, c.portal_aceite_versao = agora(), TERMO_VERSAO
        db.session.commit()
        return jsonify(ok=True)

    @app.put("/api/c/<token>/dados")
    def portal_dados_editar(token):
        c, conta, m = _portal(token)
        d = _json()
        try:
            c.empresa = _txt(d, "nome", 160, True, "Nome")
            c.telefone = _txt(d, "telefone", 40)
            c.endereco = _txt(d, "endereco", 300)
            email = _txt(d, "email", 254)
            if email and not EMAIL_RE.match(email):
                raise ValueError("E-mail inválido.")
            c.email = email
            editaveis = [x for x in m["campos_cliente"] if x.get("portal") and not x.get("interno")]
            if editaveis:
                c.dados = validar_dados(editaveis, d.get("dados"), c.dados)
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        db.session.add(PortalMsg(conta_id=conta.id, cliente_id=c.id, de="cliente", tipo="dados",
                                 texto="Atualizou os dados cadastrais pela área do cliente.", lida=False))
        db.session.commit()
        return jsonify(ok=True)

    @app.post("/api/c/<token>/mensagens")
    def portal_mensagem(token):
        c, conta, _ = _portal(token)
        f = request.form if request.form else _json()
        if not limitar(f"pmsg:{c.id}", 20, 3600):
            return _erro("Muitas mensagens seguidas. Tente de novo daqui a pouco.", 429)
        try:
            texto = _txt(f, "texto", 2000)
        except ValueError as e:
            return _erro(str(e))
        tipo = f.get("tipo") if f.get("tipo") in ("mensagem", "pedido", "receita") else "mensagem"
        arq, rel, nome, mime, tam = request.files.get("arquivo"), None, None, None, None
        if arq and arq.filename:
            ext = Path(arq.filename).suffix.lower()
            if ext not in EXT_ANEXO:
                return _erro("Envie PDF, PNG, JPG ou WEBP.")
            cab = arq.stream.read(16)
            arq.stream.seek(0)
            if not _assinatura_ok(ext, cab):
                return _erro("O arquivo não parece ser um " + ext[1:].upper() + " de verdade.")
            (DATA_DIR / "uploads" / "contas" / str(conta.id) / "portal").mkdir(parents=True, exist_ok=True)
            rel = f"contas/{conta.id}/portal/{secrets.token_hex(12)}{ext}"
            arq.save(DATA_DIR / "uploads" / rel)
            nome, mime, tam = secure_filename(arq.filename)[:200] or f"arquivo{ext}", EXT_ANEXO[ext], (DATA_DIR / "uploads" / rel).stat().st_size
        if not texto and not rel:
            return _erro("Escreva uma mensagem ou anexe um arquivo.")
        db.session.add(PortalMsg(conta_id=conta.id, cliente_id=c.id, de="cliente", tipo=tipo, texto=texto or "", arquivo=rel,
                                 nome=nome, mime=mime, tamanho=tam, lida=False))
        db.session.commit()
        return jsonify(ok=True)

    @app.get("/api/c/<token>/arquivo/<tipo>/<int:aid>")
    def portal_arquivo(token, tipo, aid):
        c, conta, m = _portal(token)
        if tipo == "anexo":
            a = db.session.get(Anexo, aid)
            r = db.session.get(Registro, a.registro_id) if a else None
            tp = next((t for t in m["tipos_registro"] if r and t["id"] == r.tipo), None)
            if not a or r.cliente_id != c.id or not tp or not tp.get("portal", True):
                abort(404)
            rel, mime, nome = a.arquivo, a.mime, a.nome
        elif tipo == "msg":
            x = db.session.get(PortalMsg, aid)
            if not x or x.cliente_id != c.id or not x.arquivo:
                abort(404)
            rel, mime, nome = x.arquivo, x.mime, x.nome
        else:
            abort(404)
        resp = send_from_directory(DATA_DIR / "uploads", rel, mimetype=mime, download_name=nome)
        resp.headers["Cache-Control"] = "private, no-store"
        return resp

    # ── pagamentos do registro ─────────────────────────────────────────────
    @app.post("/api/n/registros/<int:rid>/pagamentos")
    @login_obrigatorio
    def n_pg_criar(rid):
        r, d = _n_registro(rid), _json()
        t = _tipos().get(r.tipo)
        try:
            if not t or not t["pagamentos"]:
                raise ValueError("Este tipo de registro não usa pagamentos.")
            valor = _centavos(d.get("valor_cent"), "Valor")
            if valor <= 0:
                raise ValueError("Informe um valor maior que zero.")
            p = RegistroPagamento(registro_id=r.id, descricao=_txt(d, "descricao", 120) or "Pagamento",
                                  valor_cent=valor, vence_em=_data(d.get("vence_em")) or hoje())
            db.session.add(p)
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        return jsonify(ok=True, pagamento=_n_pg_json(p))

    def _n_pg(pid):
        p = db.session.get(RegistroPagamento, pid)
        if not p:
            abort(404)
        _n_registro(p.registro_id)                 # herda a checagem de conta do registro
        return p

    @app.post("/api/n/pagamentos/<int:pid>/receber")
    @login_obrigatorio
    def n_pg_receber(pid):
        p, d = _n_pg(pid), _json()
        try:
            total = min(_centavos(d.get("total_recebido_cent"), "Valor recebido"), p.valor_cent)
            data = _data(d.get("data"))
        except ValueError as e:
            return _erro(str(e))
        forma = d.get("forma") or None
        if forma is not None and forma not in FORMAS_PG:
            return _erro("Forma de pagamento inválida.")
        try:
            parcelas = int(d.get("parcelas") or 1) if forma == "credito" else None
        except (TypeError, ValueError):
            return _erro("Número de parcelas inválido.")
        if parcelas is not None and not 1 <= parcelas <= 12:
            return _erro("Parcelas: de 1 a 12.")
        p.valor_recebido_cent = total                      # define o TOTAL recebido (aceita parcial)
        p.pago_em = (data or hoje()) if total > 0 else None
        p.forma, p.parcelas = (forma, parcelas) if total > 0 else (None, None)
        p.taxa_cent = taxa_do_pagamento(conta_do(usuario_atual()), forma, parcelas, total) if total > 0 else 0
        db.session.commit()
        return jsonify(ok=True, pagamento=_n_pg_json(p))

    @app.delete("/api/n/pagamentos/<int:pid>")
    @login_obrigatorio
    def n_pg_apagar(pid):
        p = _n_pg(pid)
        if p.valor_recebido_cent > 0:
            return _erro("Pagamento com valor recebido não pode ser removido. Zere o recebimento antes.", 409)
        db.session.delete(p)
        db.session.commit()
        return jsonify(ok=True)

    # ── produtos e precificação ────────────────────────────────────────────
    def _n_produto(pid):
        p = db.session.get(Produto, pid)
        u = usuario_atual()
        if not p or p.conta_id != u.conta_id or (not _n_equipe() and p.owner_id != u.id):
            abort(404)
        return p

    def _n_prod_json(p):
        quantidade = 100 if p.unidade_venda == "cento" else 1
        custo_total = p.custo_fixo_cent + p.custo_variavel_cent
        custo_unit = round(custo_total / quantidade)
        preco_unit = round(p.preco_venda_cent / quantidade)
        lucro = p.preco_venda_cent - custo_total
        sugerido = round(custo_total * (1 + p.lucro_pct / 100))
        return dict(id=p.id, nome=p.nome, unidade_venda=p.unidade_venda,
                    custo_fixo_cent=p.custo_fixo_cent, custo_variavel_cent=p.custo_variavel_cent,
                    custo_total_cent=custo_total, custo_unit_cent=custo_unit,
                    preco_venda_cent=p.preco_venda_cent, preco_unit_cent=preco_unit,
                    lucro_cent=lucro, lucro_pct=p.lucro_pct, preco_sugerido_cent=sugerido,
                    observacoes=p.observacoes, criado_em=_fmt(p.criado_em))

    def _n_prod_preencher(p, d, novo=False):
        nome = _txt(d, "nome", 160, novo or "nome" in d, "Nome")
        if nome:
            p.nome = nome
        unidade = d.get("unidade_venda", p.unidade_venda or "unidade")
        if unidade not in ("unidade", "cento"):
            raise ValueError("Escolha vender por unidade ou por cento.")
        p.unidade_venda = unidade
        for campo, chave in (("custo_fixo_cent", "custo_fixo_cent"),
                             ("custo_variavel_cent", "custo_variavel_cent"),
                             ("preco_venda_cent", "preco_venda_cent")):
            if chave in d:
                setattr(p, campo, _centavos(d[chave], "Valor"))
        if "lucro_pct" in d:
            pct = float(d["lucro_pct"])
            if not math.isfinite(pct) or pct < 0 or pct > 10000:
                raise ValueError("O percentual de lucro deve ficar entre 0 e 10.000%.")
            p.lucro_pct = pct
        if "observacoes" in d:
            p.observacoes = _txt(d, "observacoes", 500) or ""

    @app.get("/api/n/produtos")
    @login_obrigatorio
    def n_produtos():
        stmt = select(Produto).where(Produto.conta_id == usuario_atual().conta_id)
        if not _n_equipe():
            stmt = stmt.where(Produto.owner_id == usuario_atual().id)
        q = (request.args.get("q") or "").strip()[:80]
        if q:
            stmt = stmt.where(Produto.nome.icontains(q, autoescape=True))
        produtos = db.session.scalars(stmt.order_by(Produto.nome)).all()
        return jsonify(ok=True, produtos=[_n_prod_json(p) for p in produtos])

    @app.post("/api/n/produtos")
    @login_obrigatorio
    def n_produto_criar():
        d, u = _json(), usuario_atual()
        try:
            p = Produto(conta_id=u.conta_id, owner_id=u.id, nome="")
            _n_prod_preencher(p, d, True)
            db.session.add(p)
            db.session.commit()
        except (ValueError, TypeError) as e:
            db.session.rollback()
            return _erro(str(e))
        return jsonify(ok=True, produto=_n_prod_json(p))

    @app.put("/api/n/produtos/<int:pid>")
    @login_obrigatorio
    def n_produto_editar(pid):
        p, d = _n_produto(pid), _json()
        try:
            _n_prod_preencher(p, d)
            p.atualizado_em = agora()
            db.session.commit()
        except (ValueError, TypeError) as e:
            db.session.rollback()
            return _erro(str(e))
        return jsonify(ok=True, produto=_n_prod_json(p))

    @app.delete("/api/n/produtos/<int:pid>")
    @login_obrigatorio
    def n_produto_apagar(pid):
        db.session.delete(_n_produto(pid))
        db.session.commit()
        return jsonify(ok=True)

    # ── resumo e agenda ────────────────────────────────────────────────────
    def _n_ativ_visiveis(stmt):
        stmt = stmt.where(Atividade.user_id.in_(_n_ids_users()))
        return stmt if _n_equipe() else stmt.where(Atividade.user_id == usuario_atual().id)

    @app.get("/api/n/resumo")
    @login_obrigatorio
    def n_resumo():
        hj = hoje()
        mes_ini = hj.replace(day=1)
        mes_fim = _add_meses(mes_ini, 1)
        tipos = []
        for t in _M()["tipos_registro"]:
            stmt = _n_visivel(select(Registro.status, func.count(), func.coalesce(func.sum(Registro.valor_cent), 0))
                              .where(Registro.tipo == t["id"]), Registro).group_by(Registro.status)
            linhas = {s: (n, int(v)) for s, n, v in db.session.execute(stmt)}
            etapas = [dict(etapa=e, qtd=linhas.get(e, (0, 0))[0], valor_cent=linhas.get(e, (0, 0))[1],
                           final=e in t["etapas_finais"]) for e in t["etapas"]]
            tipos.append(dict(id=t["id"], rotulo=t["rotulo"], rotulo_plural=t["rotulo_plural"], etapas=etapas,
                              em_aberto=sum(x["qtd"] for x in etapas if not x["final"])))

        P = RegistroPagamento

        def soma(expr, *filtros):
            q = (select(func.coalesce(func.sum(expr), 0)).select_from(P)
                 .join(Registro, Registro.id == P.registro_id).where(*filtros))
            return int(db.session.scalar(_n_visivel(q, Registro)))

        financeiro = dict(
            previsto_mes_cent=soma(P.valor_cent, P.vence_em >= mes_ini, P.vence_em < mes_fim),
            recebido_mes_cent=soma(P.valor_recebido_cent, P.pago_em >= mes_ini, P.pago_em < mes_fim),
            taxas_mes_cent=soma(P.taxa_cent, P.pago_em >= mes_ini, P.pago_em < mes_fim),
            custos_mes_cent=int(db.session.scalar(_n_visivel(
                select(func.coalesce(func.sum(CustoCliente.valor_cent), 0)).select_from(CustoCliente)
                .join(Cliente, Cliente.id == CustoCliente.cliente_id)
                .where(CustoCliente.data >= mes_ini, CustoCliente.data < mes_fim), Cliente))),
            em_atraso_cent=soma(P.valor_cent - P.valor_recebido_cent, P.vence_em < hj, P.valor_recebido_cent < P.valor_cent),
            a_receber_cent=soma(P.valor_cent - P.valor_recebido_cent, P.valor_recebido_cent < P.valor_cent))

        proximos = []
        for t in _M()["tipos_registro"]:
            if not t["data_ref"]:
                continue
            stmt = _n_visivel(select(Registro, Cliente.empresa).join(Cliente, Cliente.id == Registro.cliente_id)
                              .where(Registro.tipo == t["id"], Registro.data_ref >= hj,
                                     Registro.status.notin_(t["etapas_finais"])), Registro)
            proximos += [_n_reg_json(r, nome) for r, nome in
                         db.session.execute(stmt.order_by(Registro.data_ref, Registro.id).limit(8))]
        proximos.sort(key=lambda x: x["data_ref"])

        pend = _n_ativ_visiveis(select(func.count()).select_from(Atividade).where(
            Atividade.registro_id.is_not(None), Atividade.feita_em.is_(None), Atividade.vence_em <= agora_local()))
        clientes = db.session.scalar(_n_visivel(select(func.count()).select_from(Cliente), Cliente))
        novas = db.session.execute(_n_visivel(
            select(PortalMsg, Cliente.empresa).join(Cliente, Cliente.id == PortalMsg.cliente_id)
            .where(PortalMsg.de == "cliente", PortalMsg.lida.is_(False)), Cliente).order_by(PortalMsg.id.desc()).limit(8)).all()
        return jsonify(ok=True, clientes=clientes, tipos=tipos, financeiro=financeiro, proximos=proximos[:8],
                       msgs_novas=[dict(id=m.id, cliente_id=m.cliente_id, cliente_nome=nome, tipo=m.tipo,
                                        texto=(m.texto or "")[:90], em=_fmt(m.criado_em)) for m, nome in novas],
                       lembretes_pendentes=db.session.scalar(pend))

    @app.get("/api/n/agenda")
    @login_obrigatorio
    def n_agenda():
        try:
            dias = max(1, min(int(request.args.get("dias", 14)), 365))
        except ValueError:
            dias = 14
        stmt = (select(Atividade, Cliente.empresa).outerjoin(Cliente, Cliente.id == Atividade.cliente_id)
                .where(Atividade.lead_id.is_(None), Atividade.feita_em.is_(None), Atividade.vence_em.is_not(None),
                       Atividade.vence_em <= agora_local() + timedelta(days=dias)))
        rows = db.session.execute(_n_ativ_visiveis(stmt).order_by(Atividade.vence_em).limit(200))
        return jsonify(ok=True, atividades=[_n_ativ_json(a, nome) for a, nome in rows])

    @app.post("/api/n/atividades")
    @login_obrigatorio
    def n_ativ_criar():
        d, u = _json(), usuario_atual()
        try:
            titulo = _txt(d, "titulo", 200, True, "Título")
            try:
                vence = datetime.fromisoformat(d.get("vence_em") or "")
            except (TypeError, ValueError):
                raise ValueError("Informe quando (AAAA-MM-DD ou AAAA-MM-DDTHH:MM).")
            c = r = None
            if d.get("registro_id") is not None:
                r = _n_registro(_int_id(d["registro_id"], "Registro"))
                c = db.session.get(Cliente, r.cliente_id)
            elif d.get("cliente_id") is not None:
                c = _n_cliente(_int_id(d["cliente_id"], "Cadastro"))
            a = Atividade(user_id=u.id, titulo=titulo, vence_em=vence, cliente_id=c.id if c else None,
                          registro_id=r.id if r else None)
            db.session.add(a)
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            return _erro(str(e))
        return jsonify(ok=True, atividade=_n_ativ_json(a, c.empresa if c else None))

    @app.post("/api/n/atividades/<int:aid>/feita")
    @login_obrigatorio
    def n_ativ_feita(aid):
        u = usuario_atual()
        a, d = db.session.get(Atividade, aid), _json()
        dono = db.session.get(User, a.user_id) if a else None
        if not a or not dono or dono.conta_id != u.conta_id or not (_n_equipe() or a.user_id == u.id):
            abort(404)
        a.feita_em = agora() if d.get("feita", True) else None
        db.session.commit()
        return jsonify(ok=True, atividade=_n_ativ_json(a))

    # ── mensagens (modelos do manifesto) e IA ──────────────────────────────
    def _n_ctx_msg(c, r=None):
        u, M = usuario_atual(), _M()
        ctx = dict(dados_legiveis(M["campos_cliente"], c.dados))
        if r and _tipos().get(r.tipo):
            ctx.update(dados_legiveis(_tipos()[r.tipo]["campos"], r.dados))
        ctx.update(nome=c.empresa, primeiro_nome=(c.empresa.split() or [""])[0], telefone=c.telefone or "",
                   atendente=u.nome, negocio=nome_app_do(u))
        if r:
            ctx.update(titulo=r.titulo, status=r.status, valor=_brl(r.valor_cent), data=_data_br(r.data_ref))
        return ctx

    def _n_alvo(d):
        """cadastro (+ registro opcional) citados no corpo da requisição."""
        c = _n_cliente(_int_id(d.get("cliente_id"), "Cadastro"))
        r = None
        if d.get("registro_id") is not None:
            r = _n_registro(_int_id(d["registro_id"], "Registro"))
            if r.cliente_id != c.id:
                raise ValueError("Esse registro é de outro cadastro.")
        return c, r

    @app.post("/api/n/mensagem")
    @login_obrigatorio
    def n_mensagem():
        d = _json()
        try:
            c, r = _n_alvo(d)
            modelo = _M()["mensagens"].get(d.get("modelo"))
            if modelo is None:
                raise ValueError("Modelo de mensagem inexistente.")
        except ValueError as e:
            return _erro(str(e))
        texto = _preencher(modelo, _n_ctx_msg(c, r))
        return jsonify(ok=True, texto=texto, whatsapp=wa_link(c.telefone, texto))

    @app.post("/api/n/ia/mensagem")
    @login_obrigatorio
    def n_ia_mensagem():
        d, u = _json(), usuario_atual()
        try:
            c, r = _n_alvo(d)
            instrucao = _txt(d, "instrucao", 300)
        except ValueError as e:
            return _erro(str(e))
        chave = chave_do_vendedor(u, "groq") or _env("GROQ_API_KEY")      # chave pessoal ou a do servidor
        if not chave:
            return _erro("Cadastre a chave Groq em Perfil (ou peça ao administrador para definir GROQ_API_KEY).")
        if not limitar(f"groq:{u.id}", 60, 3600):
            return _erro("Limite de 60 mensagens por hora atingido.", 429)
        M = _M()
        canal = "e-mail" if d.get("canal") == "email" else "WhatsApp"
        ctx = _n_ctx_msg(c, r)
        base = M["mensagens"].get(d.get("modelo"))
        if base:
            prompt = (f"Reescreva esta mensagem de {canal} em português do Brasil, natural e curta (até 90 palavras), "
                      f"mantendo exatamente os mesmos fatos (nomes, datas e valores):\n\n{_preencher(base, ctx)}")
        else:
            # LGPD: só vão para o provedor de IA nome e fatos básicos do registro, nunca o conteúdo de `dados`.
            fatos = "".join(f" {k}: {ctx[k]}." for k in ("titulo", "status", "data", "valor") if r and ctx.get(k))
            prompt = (f"Escreva uma mensagem de {canal} em português do Brasil, curta (até 90 palavras), cordial e sem "
                      f"exageros, para {c.empresa}.{fatos} Assine como {u.nome} ({nome_app_do(u)}). Não invente dados.")
        if instrucao:
            prompt += f" Instrução adicional: {instrucao}"
        persona = M["ia"]["persona"] or "Você é atendente de um pequeno negócio brasileiro, cordial e objetivo."
        try:
            texto = groq_texto(chave, prompt, persona + " Responda apenas com o texto da mensagem.")
        except RuntimeError as e:
            return _erro(str(e), 502)
        registrar_uso(u, "groq")
        db.session.commit()
        return jsonify(ok=True, texto=texto, whatsapp=wa_link(c.telefone, texto))

    # ── erros em JSON para /api ────────────────────────────────────────────
    @app.errorhandler(404)
    def _e404(_e):
        return (jsonify(ok=False, erro="Não encontrado."), 404) if request.path.startswith("/api/") else ("Não encontrado.", 404)

    @app.errorhandler(405)
    def _e405(_e):
        return jsonify(ok=False, erro="Método não permitido."), 405

    @app.errorhandler(413)
    def _e413(_e):
        return jsonify(ok=False, erro="Arquivo grande demais."), 413

    @app.errorhandler(500)
    def _e500(_e):
        db.session.rollback()
        return jsonify(ok=False, erro="Erro interno. Tente de novo."), 500

    return app


app = criar_app()


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════
def init_db():
    """Idempotente: cria tabelas, aplica migrações, semeia config e sincroniza o admin com o .env."""
    with app.app_context():
        db.create_all()
        migrar()
        info = db.session.get(SchemaInfo, 1)
        if info is None:
            db.session.add(SchemaInfo(id=1, versao=SCHEMA_VERSAO))
        elif info.versao < SCHEMA_VERSAO:
            # futuras migrações: _add_col("tabela", "coluna", "TIPO DEFAULT x")
            info.versao = SCHEMA_VERSAO
        for k, v in CONFIG_PADRAO.items():
            if db.session.get(Config, k) is None:
                db.session.add(Config(chave=k, valor=v))
        db.session.commit()

        email, senha = _norm(_env("ADMIN_EMAIL")), os.environ.get("ADMIN_PASS", "")
        if email and senha:
            u = db.session.scalar(select(User).where(User.email_norm == email))
            if u is None:
                db.session.add(User(nome=_env("ADMIN_NOME") or "Administrador", email=email, email_norm=email,
                                    papel="admin", senha_hash=generate_password_hash(senha)))
            else:
                u.papel, u.status = "admin", "ativo"
                if not check_password_hash(u.senha_hash, senha):
                    u.senha_hash = generate_password_hash(senha)
                    u.sessao_versao += 1
            db.session.commit()
        elif not db.session.scalar(select(User).where(User.papel == "admin")):
            print("AVISO: defina ADMIN_EMAIL e ADMIN_PASS no .env para criar o administrador.", file=sys.stderr)
        manter_mensalidades_todas()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "init-db":
        init_db()
        print("banco ok")
    elif cmd == "check-nicho":
        for nid, m in NICHOS.items():
            print(f"ok   {nid}: módulos {', '.join(m['modulos'])}" + ("" if nicho_serve_conta(m) else "  (só para quem não tem conta)"))
            for t in m["tipos_registro"]:
                print(f"       - {t['id']}: {len(t['campos'])} campos · etapas: {' > '.join(t['etapas'])}"
                      f"{' · pagamentos' if t['pagamentos'] else ''}{' · ' + str(len(t['lembretes'])) + ' lembrete(s)' if t['lembretes'] else ''}")
        for pasta, erro in NICHOS_ERRO.items():
            print(f"ERRO {pasta}: {erro}")
        sys.exit(1 if NICHOS_ERRO else 0)
    elif cmd == "run":
        init_db()
        app.run(host=_env("HOST") or "127.0.0.1", port=_int("PORT", 8000), debug=DEBUG)
    else:
        sys.exit("Uso: python app.py [run | init-db | check-nicho]")
