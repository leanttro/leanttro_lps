"""parceiros.py — Parceiros da Leanttro (cliente antigo, revendedor e indicador) dentro do leanttro_lps.

Tudo que é de parceiro mora AQUI. O app.py só tem 2 linhas em create_app():
    import parceiros
    parceiros.registrar(app)

Como funciona:
  • Tenant dedicado (env PARCEIROS_TENANT, padrão "lpparceiros"), ex.: leanttro.com/parceiros
  • A central (3 cards) é o index.html do site sites/lp_parceiros/ (público).
  • Rotas do parceiro (só nesse tenant):
        /parceiro/entrar        login por código (campo único, sem senha)
        /parceiro/valores       Valores Exclusivos          (card 1)
        /parceiro/loja          Minha Loja de Sites         (card 2)
        /parceiro/indicacoes    formulário de Indicações    (card 3)
        /p/<slug>               página de vendas pública do revendedor
  • Qualquer tela sem sessão cai em /parceiro/entrar e, depois do código, vai para o destino pedido.
  • Rotas suas (só no ADMIN_HOST, logado no super-admin): /_admin/parceiros/  — uma tela só, com abas.
  • Código de acesso: gerado por você no admin (longo e aleatório). Para trocar/cortar o acesso: gerar novo ou revogar.
  • Logo: upload usando o volume /data do lps (mesmo mecanismo dos slots de imagem).
  • Valores em CENTAVOS (inteiros) em todo o banco.

Templates usados (templates/parceiros/):
  entrar.html, aviso.html, valores.html, loja.html, indicar.html, minisite.html, admin.html
"""
import importlib
import os
import re
import secrets
import sys
from datetime import date, timedelta
from functools import wraps

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import select, func

# ── pega as funções do app.py sem criar um segundo módulo (python app.py init-db roda como __main__) ──
_m = sys.modules.get("__main__")
if not hasattr(_m, "Tenant"):
    _m = importlib.import_module("app")

db = _m.db
Tenant = _m.Tenant
agora_utc = _m.agora_utc
exigir_csrf = _m.exigir_csrf
eh_email = _m.eh_email
so_digitos = _m.so_digitos
normalizar_whatsapp = _m.normalizar_whatsapp
wa_link = _m.wa_link
gerar_slug = _m.gerar_slug
permitir = _m.permitir
indice = _m.indice
salvar_slot = _m.salvar_slot
remover_slot = _m.remover_slot
snap_largura = _m.snap_largura
UploadError = _m.UploadError
ErroNegocio = _m.ErroNegocio

# ═══════════════════════════════════════════════════════════════════════════
# REGRAS DE NEGÓCIO (mexa só aqui)
# ═══════════════════════════════════════════════════════════════════════════
WA_LEANTTRO = "5511913324827"      # WhatsApp da Leanttro (todos os botões da página de valores)
TIPOS = {"cliente_antigo": "Cliente antigo", "revendedor": "Revendedor", "indicador": "Indicador"}
COMISSAO_REVENDA_PCT = 50          # % do valor público do setup
COMISSAO_INDICACAO_PCT = 30        # % do setup (valor público)
MENSAL_REVENDEDOR_CENT = 3000      # R$ 30,00 fixos por mês, por cliente com plano ativo
EXCLUSIVIDADE_DIAS = 30            # quem registrou o lead primeiro fica com ele
MAX_EXTRAS = 12

# Preço de parceiro = o que está na sua tabela. Preço público = o que a página do revendedor mostra.
# ⚠ CONFIRMAR: os públicos abaixo são 2× o de parceiro (50%); plano R$ 59 e seção extra R$ 100 vêm da conversa.
CATALOGO = {
    "lp_basica":   {"nome": "Landing Básica",    "parceiro": 35000, "publico": 70000,
                    "inclui": "7 seções, celular, WhatsApp e formulário, aviso por e-mail, SEO básico, 1 rodada de ajustes, contrato e NF"},
    "lp_completa": {"nome": "Landing Completa",  "parceiro": 60000, "publico": 120000,
                    "inclui": "Até 12 seções, painel de contatos, Pixel e Analytics, texto escrito ou revisado, 2 a 3 rodadas, 1 mês de suporte, contrato e NF"},
    "site_prof":   {"nome": "Site profissional", "parceiro": 85000, "publico": 170000,
                    "inclui": "Até 5 páginas, painel admin, blog, contatos no banco, SEO básico, contrato e NF"},
}
EXTRA = {"nome": "Seção extra", "parceiro": 5000, "publico": 10000, "inclui": "Em qualquer landing page"}
PLANO = {"nome": "Plano mensal Básico", "parceiro": 3000, "publico": 5900,
         "inclui": "Hospedagem, 1 alteração simples (frontend) por mês, entrega em até 3 dias úteis"}

# Só aparecem na página de valores (não entram na página do revendedor nem em pedido).
TABELA_EXTRA = [
    ("Sistema sob medida", None, "Sob orçamento — revenda não, só indicação"),
    ("Site 3D", None, "Sob orçamento — revenda não, só indicação"),
    ("Painel de contatos (após a Básica)", "a partir de R$ 30/mês", ""),
    ("Alteração básica", "R$ 40", "Só frontend: cor, imagem, texto, layout"),
    ("Alteração intermediária", "R$ 70", "Backend + frontend"),
    ("Alteração com banco", "R$ 100", "Quando precisa alterar tabela do banco"),
    ("Suporte e serviços avulsos", "R$ 35/hora", "Acesso a servidor, configuração, integração, diagnóstico, call, consultoria"),
    ("Texto longo, foto, logo", "Orçado à parte", ""),
    ("Domínio", "Por conta do cliente", ""),
    ("Sites e sistemas não feitos pela Leanttro", "Orçado à parte", ""),
]
INTERESSES = {"lp": "Landing page", "site": "Site profissional", "sistema": "Sistema sob medida",
              "3d": "Site 3D", "outro": "Outro / não sei"}
STATUS_INDICACAO = {"registrada": "Registrada", "em_negociacao": "Em negociação", "fechada": "Fechada", "perdida": "Perdida"}
STATUS_PEDIDO = {"novo": "Novo", "aguardando_pagamento": "Aguardando pagamento", "pago": "Pago",
                 "entregue": "Entregue", "cancelado": "Cancelado"}
ABAS = {"parceiros": "Parceiros", "indicacoes": "Indicações", "pedidos": "Pedidos", "repasses": "Repasses"}
COR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")


def brl(cent):
    """1234500 → 'R$ 12.345,00' (filtro de template: {{ valor|brl }})."""
    cent = int(cent or 0)
    s = f"{abs(cent) // 100:,}".replace(",", ".") + f",{abs(cent) % 100:02d}"
    return ("-" if cent < 0 else "") + "R$ " + s


def _reais_para_cent(txt):
    t = (txt or "").strip().replace("R$", "").replace(" ", "")
    if not t:
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        v = round(float(t) * 100)
    except ValueError:
        raise ErroNegocio("Valor inválido. Use algo como 1500 ou 1.500,00.")
    if v < 0 or v > 10_000_000_00:
        raise ErroNegocio("Valor fora do limite.")
    return int(v)


def _txt(v, n):
    return re.sub(r"\s+", " ", (v or "").strip())[:n]


def _data(v):
    try:
        return date.fromisoformat((v or "").strip()) if (v or "").strip() else None
    except ValueError:
        raise ErroNegocio("Data inválida.")


def _hoje():
    return agora_utc().date()


# ═══════════════════════════════════════════════════════════════════════════
# MODELOS (tabelas novas; nenhuma tabela existente é alterada — db.create_all() cria sozinho)
# ═══════════════════════════════════════════════════════════════════════════
class Parceiro(db.Model):
    __tablename__ = "parceiros"
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(254), unique=True, nullable=False, index=True)
    whatsapp = db.Column(db.String(20), nullable=False)
    tipo = db.Column(db.String(20), nullable=False)
    ativo = db.Column(db.Boolean, nullable=False, default=True)
    preco_ate = db.Column(db.Date)                                   # fim da vigência do preço de parceiro
    aceite_uso = db.Column(db.Boolean, nullable=False, default=False)  # autorização de uso do projeto como exemplo
    aceite_em = db.Column(db.Date)
    codigo = db.Column(db.String(64), unique=True, index=True)       # código de acesso (None = sem acesso)
    token_versao = db.Column(db.Integer, nullable=False, default=1)  # subir = derruba as sessões abertas
    # página de vendas (revendedor)
    slug = db.Column(db.String(40), unique=True)
    nome_site = db.Column(db.String(80))
    bio = db.Column(db.String(300))
    cor = db.Column(db.String(7), nullable=False, default="#0f766e")
    obs = db.Column(db.Text)
    criado_em = db.Column(db.DateTime, nullable=False, default=agora_utc)

    @property
    def vigente(self):
        return self.preco_ate is None or self.preco_ate >= _hoje()

    @property
    def pode_ver_tabela(self):
        return self.ativo and self.tipo in ("cliente_antigo", "revendedor") and self.aceite_uso and self.vigente

    @property
    def pode_vender(self):
        return self.ativo and self.tipo == "revendedor" and self.aceite_uso and self.vigente

    @property
    def pode_indicar(self):
        return self.ativo

    @property
    def tipo_nome(self):
        return TIPOS.get(self.tipo, self.tipo)

    @property
    def exibicao(self):
        return self.nome_site or self.nome


class Indicacao(db.Model):
    __tablename__ = "parceiro_indicacoes"
    id = db.Column(db.Integer, primary_key=True)
    parceiro_id = db.Column(db.Integer, db.ForeignKey("parceiros.id", ondelete="CASCADE"), nullable=False, index=True)
    nome = db.Column(db.String(120), nullable=False)          # pessoa ou empresa indicada
    telefone = db.Column(db.String(30), nullable=False)
    chave = db.Column(db.String(20), nullable=False, index=True)  # últimos 11 dígitos → detecta duplicidade
    interesse = db.Column(db.String(20), nullable=False, default="outro")
    obs = db.Column(db.String(500))
    status = db.Column(db.String(20), nullable=False, default="registrada")
    valor_setup = db.Column(db.Integer)                        # centavos, preenchido por você ao fechar
    expira_em = db.Column(db.DateTime, nullable=False)         # fim da exclusividade
    criado_em = db.Column(db.DateTime, nullable=False, default=agora_utc)
    parceiro = db.relationship("Parceiro", lazy="joined")

    @property
    def em_exclusividade(self):
        return self.status == "fechada" or (self.status in ("registrada", "em_negociacao") and self.expira_em > agora_utc())


class Pedido(db.Model):
    __tablename__ = "parceiro_pedidos"
    id = db.Column(db.Integer, primary_key=True)
    parceiro_id = db.Column(db.Integer, db.ForeignKey("parceiros.id", ondelete="CASCADE"), nullable=False, index=True)
    cliente_nome = db.Column(db.String(120), nullable=False)
    cliente_telefone = db.Column(db.String(30), nullable=False)
    cliente_email = db.Column(db.String(254))
    item = db.Column(db.String(20), nullable=False)
    extras = db.Column(db.Integer, nullable=False, default=0)
    plano = db.Column(db.Boolean, nullable=False, default=False)
    valor_cent = db.Column(db.Integer, nullable=False)          # valor final ao cliente (preço público)
    comissao_cent = db.Column(db.Integer, nullable=False)       # 50% do setup
    mensal_cent = db.Column(db.Integer, nullable=False, default=0)  # R$ 30/mês p/ o parceiro, se plano
    obs = db.Column(db.String(500))
    status = db.Column(db.String(24), nullable=False, default="novo")
    criado_em = db.Column(db.DateTime, nullable=False, default=agora_utc)
    parceiro = db.relationship("Parceiro", lazy="joined")

    @property
    def item_nome(self):
        return CATALOGO.get(self.item, {}).get("nome", self.item)


class Repasse(db.Model):
    """O que você deve pagar ao parceiro (um registro por comissão)."""
    __tablename__ = "parceiro_repasses"
    __table_args__ = (db.UniqueConstraint("pedido_id", "indicacao_id", "referencia", name="uq_repasse_ref"),)
    id = db.Column(db.Integer, primary_key=True)
    parceiro_id = db.Column(db.Integer, db.ForeignKey("parceiros.id", ondelete="CASCADE"), nullable=False, index=True)
    pedido_id = db.Column(db.Integer, db.ForeignKey("parceiro_pedidos.id", ondelete="CASCADE"))
    indicacao_id = db.Column(db.Integer, db.ForeignKey("parceiro_indicacoes.id", ondelete="CASCADE"))
    tipo = db.Column(db.String(12), nullable=False)             # setup | mensal | indicacao
    referencia = db.Column(db.String(10), nullable=False, default="")  # "2026-11" nas mensais
    valor_cent = db.Column(db.Integer, nullable=False)
    pago_em = db.Column(db.DateTime)                            # None = a pagar
    criado_em = db.Column(db.DateTime, nullable=False, default=agora_utc)
    parceiro = db.relationship("Parceiro", lazy="joined")
    pedido = db.relationship("Pedido", lazy="joined")
    indicacao = db.relationship("Indicacao", lazy="joined")


# ═══════════════════════════════════════════════════════════════════════════
# COMUNS
# ═══════════════════════════════════════════════════════════════════════════
def _codigo():
    t = g.get("tenant")
    if t is not None and t.template == "lp_parceiros":
        return t.codigo
    return current_app.config["PARCEIROS_TENANT"]


def _novo_codigo():
    """Código de acesso longo e aleatório (32 caracteres), único no banco."""
    while True:
        c = secrets.token_urlsafe(24)
        if not db.session.scalar(select(Parceiro.id).where(Parceiro.codigo == c)):
            return c


def _central():
    """A central (3 cards) = raiz do tenant, respeitando o prefixo de caminho (ex.: /parceiros/)."""
    return (request.script_root or "") + "/"


def logo_url(p, w=None):
    """URL do logo do parceiro ('' se não enviou). Funciona no tenant e no super-admin."""
    r = indice(_codigo()).get(f"par{p.id}_logo")
    if not r:
        return ""
    args = {"v": r[1]}
    if w and not r[0].endswith(".svg"):
        args["w"] = snap_largura(w)
    return url_for("site.media", codigo=_codigo(), arquivo=r[0], **args)


def calcular_pedido(item, extras, plano):
    c = CATALOGO.get(item)
    if not c:
        raise ErroNegocio("Escolha um plano válido.")
    if not 0 <= extras <= MAX_EXTRAS:
        raise ErroNegocio(f"Seções extras: de 0 a {MAX_EXTRAS}.")
    if extras and item == "site_prof":
        raise ErroNegocio("Seção extra vale só para landing page.")
    valor = c["publico"] + extras * EXTRA["publico"]
    return valor, valor * COMISSAO_REVENDA_PCT // 100, (MENSAL_REVENDEDOR_CENT if plano else 0)


def _repasse_unico(p_id, tipo, valor, pedido_id=None, indicacao_id=None, referencia=""):
    r = db.session.scalar(select(Repasse).where(Repasse.pedido_id == pedido_id, Repasse.indicacao_id == indicacao_id,
                                                Repasse.referencia == referencia, Repasse.tipo == tipo))
    if r:
        if r.pago_em is None:
            r.valor_cent = valor
        return r, False
    r = Repasse(parceiro_id=p_id, tipo=tipo, valor_cent=valor, pedido_id=pedido_id, indicacao_id=indicacao_id,
                referencia=referencia)
    db.session.add(r)
    return r, True


def _totais(parceiro_id=None):
    q = select(Repasse.parceiro_id, Repasse.pago_em.is_(None), func.coalesce(func.sum(Repasse.valor_cent), 0)) \
        .group_by(Repasse.parceiro_id, Repasse.pago_em.is_(None))
    if parceiro_id:
        q = q.where(Repasse.parceiro_id == parceiro_id)
    out = {}
    for pid, a_pagar, soma in db.session.execute(q):
        d = out.setdefault(pid, {"a_pagar": 0, "pago": 0})
        d["a_pagar" if a_pagar else "pago"] += int(soma)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# ÁREA DO PARCEIRO  —  /parceiro/...   (só no tenant PARCEIROS_TENANT)
# ═══════════════════════════════════════════════════════════════════════════
bp_parceiros = Blueprint("parceiros", __name__, url_prefix="/parceiro")
ABERTAS = {"parceiros.entrar"}
# card → endpoint (usado no ?ir= do login: depois do código o parceiro volta para onde clicou)
DESTINOS = {"valores": "parceiros.valores", "loja": "parceiros.loja", "indicacoes": "parceiros.indicar"}
_DESTINO_DE = {v: k for k, v in DESTINOS.items()}


@bp_parceiros.before_request
def _antes():
    if g.get("tenant") is None or g.tenant.template != "lp_parceiros":
        abort(404)
    exigir_csrf()
    g.parceiro = None
    if request.endpoint in ABERTAS:
        return None
    s = session.get("par") or {}
    p = db.session.get(Parceiro, s.get("pid")) if s.get("pid") else None
    if not p or not p.ativo or s.get("v") != p.token_versao:
        session.pop("par", None)
        ir = _DESTINO_DE.get(request.endpoint, "") if request.method == "GET" else ""
        return redirect(url_for("parceiros.entrar", ir=ir) if ir else url_for("parceiros.entrar"))
    g.parceiro = p
    return None


def _aviso(msg, status=403):
    return render_template("parceiros/aviso.html", msg=msg, central=_central()), status


def _so(*tipos, vender=False, tabela=False, indicar=False):
    def deco(fn):
        @wraps(fn)
        def w(*a, **k):
            p = g.parceiro
            if tipos and p.tipo not in tipos:
                return _aviso("Esta área não está disponível para o seu tipo de cadastro.")
            if (vender and not p.pode_vender) or (tabela and not p.pode_ver_tabela) or (indicar and not p.pode_indicar):
                return _aviso("Este recurso não está liberado para o seu cadastro "
                              "(condições do parceiro pendentes ou vencidas). Fale com a Leanttro.")
            return fn(*a, **k)
        return w
    return deco


@bp_parceiros.route("/entrar", methods=["GET", "POST"], endpoint="entrar")
def entrar():
    ir = request.values.get("ir", "")
    if ir not in DESTINOS:
        ir = ""
    if request.method == "POST":
        if not permitir(f"parcod:{request.remote_addr}", 10, 600):
            flash("Muitas tentativas. Aguarde alguns minutos e tente de novo.", "error")
            return render_template("parceiros/entrar.html", ir=ir), 429
        cod = (request.form.get("codigo") or "").strip()
        p = db.session.scalar(select(Parceiro).where(Parceiro.codigo == cod)) if 20 <= len(cod) <= 64 else None
        if p and p.ativo:
            session.pop("par", None)
            session["par"] = {"pid": p.id, "v": p.token_versao}
            session.permanent = True
            return redirect(url_for(DESTINOS[ir]) if ir else _central())
        flash("Código inválido.", "error")
        return render_template("parceiros/entrar.html", ir=ir), 401
    return render_template("parceiros/entrar.html", ir=ir)


@bp_parceiros.post("/sair", endpoint="sair")
def sair():
    session.pop("par", None)
    return redirect(_central())


# ───── card 1: Valores Exclusivos ─────
@bp_parceiros.get("/valores", endpoint="valores")
@_so("cliente_antigo", "revendedor", tabela=True)
def valores():
    return render_template(
        "parceiros/valores.html", p=g.parceiro, CATALOGO=CATALOGO, EXTRA=EXTRA, PLANO=PLANO, extras=TABELA_EXTRA,
        WA_LEANTTRO=WA_LEANTTRO, wa=lambda txt: wa_link(WA_LEANTTRO, txt), url_indicar=url_for("parceiros.indicar"),
        central=_central())


# ───── card 3: Indicações ─────
@bp_parceiros.route("/indicacoes", methods=["GET", "POST"], endpoint="indicar")
@_so(indicar=True)
def indicar():
    p = g.parceiro
    dados = {"nome": "", "telefone": "", "interesse": request.args.get("interesse", "outro"), "obs": ""}
    if dados["interesse"] not in INTERESSES:
        dados["interesse"] = "outro"
    if request.method == "POST":
        f = request.form
        dados = {"nome": _txt(f.get("nome"), 120), "telefone": _txt(f.get("telefone"), 30),
                 "interesse": f.get("interesse") if f.get("interesse") in INTERESSES else "outro",
                 "obs": _txt(f.get("obs"), 500)}
        try:
            if len(dados["nome"]) < 2:
                raise ErroNegocio("Informe o nome da pessoa ou empresa indicada.")
            chave = so_digitos(dados["telefone"])[-11:]
            if len(chave) < 10:
                raise ErroNegocio("Informe um telefone válido, com DDD.")
            if not permitir(f"parind:{p.id}", 30, 3600):
                raise ErroNegocio("Muitas indicações em pouco tempo. Tente mais tarde.")
            ja = db.session.scalars(select(Indicacao).where(Indicacao.chave == chave)).all()
            ativa = next((i for i in ja if i.em_exclusividade), None)
            if ativa and ativa.parceiro_id == p.id:
                raise ErroNegocio("Você já indicou este contato e ele ainda está dentro do prazo.")
            if ativa:
                raise ErroNegocio("Este contato já foi indicado por outro parceiro e está em período de exclusividade.")
            db.session.add(Indicacao(parceiro_id=p.id, chave=chave, expira_em=agora_utc() + timedelta(days=EXCLUSIVIDADE_DIAS),
                                     **dados))
            db.session.commit()
            flash("Indicação registrada.", "success")
            return redirect(url_for("parceiros.indicar"))
        except ErroNegocio as e:
            db.session.rollback()
            flash(str(e), "error")
    lista = db.session.scalars(select(Indicacao).where(Indicacao.parceiro_id == p.id)
                               .order_by(Indicacao.criado_em.desc())).all()
    return render_template("parceiros/indicar.html", p=p, d=dados, lista=lista, INTERESSES=INTERESSES,
                           STATUS=STATUS_INDICACAO, pct=COMISSAO_INDICACAO_PCT, dias=EXCLUSIVIDADE_DIAS,
                           central=_central())


# ───── card 2: Minha Loja de Sites (admin da página de vendas do revendedor) ─────
def _render_loja(p, d=None):
    minisite = url_for("parceiros_pub.minisite", slug=p.slug, _external=True) if p.slug else None
    pedidos = db.session.scalars(select(Pedido).where(Pedido.parceiro_id == p.id).order_by(Pedido.criado_em.desc())).all()
    tot = _totais(p.id).get(p.id, {"a_pagar": 0, "pago": 0})
    d = d or {"cliente_nome": "", "cliente_telefone": "", "cliente_email": "", "item": "lp_basica", "extras": 0,
              "plano": False, "obs": ""}
    return render_template("parceiros/loja.html", p=p, d=d, logo=logo_url(p, 400), minisite=minisite, pedidos=pedidos,
                           tot=tot, STATUS=STATUS_PEDIDO, CATALOGO=CATALOGO, EXTRA=EXTRA, PLANO=PLANO,
                           pct=COMISSAO_REVENDA_PCT, mensal=MENSAL_REVENDEDOR_CENT, max_extras=MAX_EXTRAS,
                           central=_central())


@bp_parceiros.get("/loja", endpoint="loja")
@_so("revendedor")
def loja():
    return _render_loja(g.parceiro)


@bp_parceiros.post("/loja/salvar", endpoint="loja_salvar")
@_so("revendedor")
def loja_salvar():
    p, f = g.parceiro, request.form
    try:
        w = normalizar_whatsapp(f.get("whatsapp"))
        if not w:
            raise ErroNegocio("WhatsApp inválido (com DDD).")
        cor = (f.get("cor") or "").strip()
        if not COR_RE.match(cor):
            raise ErroNegocio("Cor inválida. Use o formato #0f766e.")
        p.whatsapp, p.cor = w, cor.lower()
        p.nome_site, p.bio = _txt(f.get("nome_site"), 80) or None, _txt(f.get("bio"), 300) or None
        db.session.commit()
        flash("Sua página de vendas foi atualizada.", "success")
    except ErroNegocio as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("parceiros.loja"))


@bp_parceiros.post("/loja/logo", endpoint="logo")
@_so("revendedor")
def logo():
    try:
        salvar_slot(_codigo(), f"par{g.parceiro.id}_logo", {"max": 600, "svg": False, "formato": "auto"},
                    request.files.get("arquivo"))
        flash("Logo atualizado.", "success")
    except UploadError as e:
        flash(str(e), "error")
    return redirect(url_for("parceiros.loja"))


@bp_parceiros.post("/loja/logo/remover", endpoint="logo_remover")
@_so("revendedor")
def logo_remover():
    remover_slot(_codigo(), f"par{g.parceiro.id}_logo")
    flash("Logo removido.", "success")
    return redirect(url_for("parceiros.loja"))


@bp_parceiros.post("/loja/pedido", endpoint="pedido_novo")
@_so("revendedor", vender=True)
def pedido_novo():
    p, f = g.parceiro, request.form
    d = {"cliente_nome": _txt(f.get("cliente_nome"), 120), "cliente_telefone": _txt(f.get("cliente_telefone"), 30),
         "cliente_email": _txt(f.get("cliente_email"), 254).lower(), "item": f.get("item", ""),
         "extras": f.get("extras", type=int, default=0), "plano": bool(f.get("plano")),
         "obs": _txt(f.get("obs"), 500)}
    try:
        if len(d["cliente_nome"]) < 2:
            raise ErroNegocio("Informe o nome do cliente.")
        if len(so_digitos(d["cliente_telefone"])) < 10:
            raise ErroNegocio("Informe o telefone/WhatsApp do cliente, com DDD.")
        if d["cliente_email"] and not eh_email(d["cliente_email"]):
            raise ErroNegocio("E-mail do cliente inválido.")
        valor, comissao, mensal = calcular_pedido(d["item"], d["extras"], d["plano"])
        if not permitir(f"parped:{p.id}", 20, 3600):
            raise ErroNegocio("Muitos pedidos em pouco tempo. Tente mais tarde.")
        db.session.add(Pedido(parceiro_id=p.id, valor_cent=valor, comissao_cent=comissao, mensal_cent=mensal, **d))
        db.session.commit()
        flash("Pedido enviado para a Leanttro.", "success")
        return redirect(url_for("parceiros.loja"))
    except ErroNegocio as e:
        db.session.rollback()
        flash(str(e), "error")
    return _render_loja(p, d)


# ═══════════════════════════════════════════════════════════════════════════
# PÁGINA DE VENDAS PÚBLICA DO REVENDEDOR  —  /p/<slug>   (preços travados no valor público)
# ═══════════════════════════════════════════════════════════════════════════
bp_publico = Blueprint("parceiros_pub", __name__)


@bp_publico.get("/p/<slug>", endpoint="minisite")
def minisite(slug):
    if g.get("tenant") is None or g.tenant.template != "lp_parceiros":
        abort(404)
    p = db.session.scalar(select(Parceiro).where(Parceiro.slug == slug.lower()))
    if not p or p.tipo != "revendedor" or not p.pode_vender:
        abort(404)

    def wa(txt):
        return wa_link(p.whatsapp, f"Olá! Vim pelo site da {p.exibicao} e quero saber mais sobre: {txt}")
    itens = [{"chave": k, "nome": c["nome"], "preco": c["publico"], "inclui": c["inclui"], "wa": wa(c["nome"])}
             for k, c in CATALOGO.items()]
    return render_template("parceiros/minisite.html", p=p, logo=logo_url(p, 400), itens=itens,
                           extra={**EXTRA, "wa": wa(EXTRA["nome"])}, plano={**PLANO, "wa": wa(PLANO["nome"])},
                           wa_geral=wa("seu serviço"))


# ═══════════════════════════════════════════════════════════════════════════
# ADMIN (você)  —  /_admin/parceiros/   (só no ADMIN_HOST, logado no super-admin). Tela única, com abas.
# ═══════════════════════════════════════════════════════════════════════════
bp_admin = Blueprint("parceiros_admin", __name__, url_prefix="/_admin/parceiros")


@bp_admin.before_request
def _antes_admin():
    if not g.get("admin_host"):
        abort(404)
    exigir_csrf()
    if not session.get("sa"):
        return redirect(url_for("superadmin.login"))
    return None


def _ou_404(modelo, i):
    o = db.session.get(modelo, i)
    if not o:
        abort(404)
    return o


def _volta(aba, **kw):
    return redirect(url_for("parceiros_admin.lista", aba=aba, **{k: v for k, v in kw.items() if v not in (None, "")}))


def _form_parceiro(f, atual=None):
    nome = _txt(f.get("nome"), 120)
    email = _txt(f.get("email"), 254).lower()
    tipo = f.get("tipo")
    if len(nome) < 2:
        raise ErroNegocio("Informe o nome.")
    if not eh_email(email):
        raise ErroNegocio("E-mail inválido.")
    if tipo not in TIPOS:
        raise ErroNegocio("Escolha o tipo do parceiro.")
    w = normalizar_whatsapp(f.get("whatsapp"))
    if not w:
        raise ErroNegocio("WhatsApp inválido (com DDD).")
    outro = db.session.scalar(select(Parceiro).where(Parceiro.email == email))
    if outro and (not atual or outro.id != atual.id):
        raise ErroNegocio("Já existe parceiro com este e-mail.")
    slug = None
    if tipo == "revendedor":
        slug = _txt(f.get("slug"), 40).lower() or gerar_slug(nome, 40)
        if not SLUG_RE.match(slug):
            raise ErroNegocio("Endereço da página de vendas inválido (letras minúsculas, números e hífen).")
        outro = db.session.scalar(select(Parceiro).where(Parceiro.slug == slug))
        if outro and (not atual or outro.id != atual.id):
            raise ErroNegocio("Este endereço de página de vendas já está em uso.")
    return dict(nome=nome, email=email, whatsapp=w, tipo=tipo, slug=slug, ativo=bool(f.get("ativo")),
                preco_ate=_data(f.get("preco_ate")), aceite_uso=bool(f.get("aceite_uso")),
                obs=(f.get("obs") or "").strip()[:2000] or None)


def _dados_de(p):
    return {"nome": p.nome, "email": p.email, "whatsapp": p.whatsapp, "tipo": p.tipo, "slug": p.slug or "",
            "ativo": p.ativo, "preco_ate": p.preco_ate.isoformat() if p.preco_ate else "", "aceite_uso": p.aceite_uso,
            "obs": p.obs or ""}


def _dados_do_form(f):
    return {"nome": f.get("nome", ""), "email": f.get("email", ""), "whatsapp": f.get("whatsapp", ""),
            "tipo": f.get("tipo", "cliente_antigo"), "slug": f.get("slug", ""), "ativo": bool(f.get("ativo")),
            "preco_ate": f.get("preco_ate", ""), "aceite_uso": bool(f.get("aceite_uso")), "obs": f.get("obs", "")}


def _acesso(p):
    """(url do login, link do WhatsApp com o código) — ou (None, None) se não tem código/tenant."""
    if not p or not p.codigo:
        return None, None
    t = db.session.scalars(select(Tenant).where(Tenant.template == "lp_parceiros")).first()
    if not t or not t.dominios_lista:
        return None, None
    url = f"https://{t.dominios_lista[0]}/parceiro/entrar"
    msg = f"Olá, {p.nome}! Seu código de acesso à Área de Parceiros da Leanttro:\n\n{p.codigo}\n\nEntre em: {url}"
    return url, wa_link(p.whatsapp, msg)


def _tela(aba="parceiros", form_p=None, form_d=None, form_aberto=False, status=None):
    """Renderiza a tela única do admin (todas as abas) com os dados da aba pedida."""
    if aba not in ABAS:
        aba = "parceiros"
    todos = db.session.scalars(select(Parceiro).order_by(Parceiro.criado_em.desc())).all()
    tot = _totais()
    n_ind = dict(db.session.execute(select(Indicacao.parceiro_id, func.count(Indicacao.id)).group_by(Indicacao.parceiro_id)).all())
    n_ped = dict(db.session.execute(select(Pedido.parceiro_id, func.count(Pedido.id)).group_by(Pedido.parceiro_id)).all())
    linhas = [{"p": p, "a_pagar": tot.get(p.id, {}).get("a_pagar", 0), "pago": tot.get(p.id, {}).get("pago", 0),
               "n_ind": n_ind.get(p.id, 0), "n_ped": n_ped.get(p.id, 0)} for p in todos]
    a_pagar_total = sum(l["a_pagar"] for l in linhas)
    contagem = {
        "parceiros": len(todos),
        "indicacoes": db.session.scalar(select(func.count(Indicacao.id)).where(Indicacao.status == "registrada")) or 0,
        "pedidos": db.session.scalar(select(func.count(Pedido.id)).where(Pedido.status == "novo")) or 0,
        "repasses": a_pagar_total,
    }
    ctx = dict(aba=aba, ABAS=ABAS, linhas=linhas, a_pagar_total=a_pagar_total, contagem=contagem, TIPOS=TIPOS,
               STATUS_IND=STATUS_INDICACAO, STATUS_PED=STATUS_PEDIDO, INTERESSES=INTERESSES,
               pct_ind=COMISSAO_INDICACAO_PCT, mes_atual=_hoje().strftime("%Y-%m"), agora=agora_utc(),
               form_p=form_p, form_d=form_d, form_aberto=form_aberto, filtro=status or "")
    if form_p:
        ctx["acesso_url"], ctx["acesso_wa"] = _acesso(form_p)
    if aba == "indicacoes":
        q = select(Indicacao).order_by(Indicacao.criado_em.desc())
        if status in STATUS_INDICACAO:
            q = q.where(Indicacao.status == status)
        ctx["indicacoes"] = db.session.scalars(q).all()
    elif aba == "pedidos":
        q = select(Pedido).order_by(Pedido.criado_em.desc())
        if status in STATUS_PEDIDO:
            q = q.where(Pedido.status == status)
        ctx["pedidos"] = db.session.scalars(q).all()
    elif aba == "repasses":
        st = status or "a_pagar"
        q = select(Repasse).order_by(Repasse.criado_em.desc())
        if st == "a_pagar":
            q = q.where(Repasse.pago_em.is_(None))
        elif st == "pago":
            q = q.where(Repasse.pago_em.is_not(None))
        lista = db.session.scalars(q).all()
        ctx.update(repasses=lista, total_repasses=sum(r.valor_cent for r in lista), filtro=st)
    return render_template("parceiros/admin.html", **ctx)


@bp_admin.get("/", endpoint="lista")
def lista():
    aba = request.args.get("aba", "parceiros")
    status = request.args.get("status", "")
    if aba == "parceiros" and request.args.get("editar", type=int):
        p = _ou_404(Parceiro, request.args.get("editar", type=int))
        return _tela("parceiros", form_p=p, form_d=_dados_de(p), form_aberto=True)
    if aba == "parceiros" and request.args.get("novo"):
        d = {"nome": "", "email": "", "whatsapp": "", "tipo": "cliente_antigo", "slug": "", "ativo": True,
             "preco_ate": (_hoje() + timedelta(days=365)).isoformat(), "aceite_uso": False, "obs": ""}
        return _tela("parceiros", form_d=d, form_aberto=True)
    return _tela(aba, status=status)


# atalhos antigos → mesma tela, na aba certa
@bp_admin.get("/indicacoes", endpoint="indicacoes")
def indicacoes():
    return _volta("indicacoes", status=request.args.get("status"))


@bp_admin.get("/pedidos", endpoint="pedidos")
def pedidos_admin():
    return _volta("pedidos", status=request.args.get("status"))


@bp_admin.get("/repasses", endpoint="repasses")
def repasses():
    return _volta("repasses", status=request.args.get("status"))


# ───── parceiros ─────
@bp_admin.route("/novo", methods=["GET", "POST"], endpoint="novo")
def novo():
    if request.method == "GET":
        return redirect(url_for("parceiros_admin.lista", aba="parceiros", novo=1))
    try:
        p = Parceiro(**_form_parceiro(request.form), codigo=_novo_codigo())
        if p.aceite_uso:
            p.aceite_em = _hoje()
        db.session.add(p)
        db.session.commit()
        flash("Parceiro criado. O código de acesso está abaixo: mande pelo WhatsApp.", "success")
        return redirect(url_for("parceiros_admin.lista", aba="parceiros", editar=p.id))
    except ErroNegocio as e:
        db.session.rollback()
        flash(str(e), "error")
        return _tela("parceiros", form_d=_dados_do_form(request.form), form_aberto=True)


@bp_admin.route("/<int:pid>", methods=["GET", "POST"], endpoint="editar")
def editar(pid):
    p = _ou_404(Parceiro, pid)
    if request.method == "GET":
        return redirect(url_for("parceiros_admin.lista", aba="parceiros", editar=pid))
    try:
        campos = _form_parceiro(request.form, p)
        if campos["aceite_uso"] and not p.aceite_uso:
            p.aceite_em = _hoje()
        for k, v in campos.items():
            setattr(p, k, v)
        db.session.commit()
        flash("Parceiro atualizado.", "success")
        return redirect(url_for("parceiros_admin.lista", aba="parceiros", editar=pid))
    except ErroNegocio as e:
        db.session.rollback()
        flash(str(e), "error")
        return _tela("parceiros", form_p=_ou_404(Parceiro, pid), form_d=_dados_do_form(request.form), form_aberto=True)


@bp_admin.post("/<int:pid>/codigo", endpoint="codigo")
def codigo(pid):
    """Gera um código novo. O anterior para de funcionar e as sessões abertas caem."""
    p = _ou_404(Parceiro, pid)
    p.codigo = _novo_codigo()
    p.token_versao += 1
    db.session.commit()
    flash("Novo código gerado. O anterior não funciona mais.", "success")
    return redirect(url_for("parceiros_admin.lista", aba="parceiros", editar=pid))


@bp_admin.post("/<int:pid>/revogar", endpoint="revogar")
def revogar(pid):
    """Corta o acesso: apaga o código e derruba as sessões abertas."""
    p = _ou_404(Parceiro, pid)
    p.codigo = None
    p.token_versao += 1
    db.session.commit()
    flash("Acesso cortado: sem código, e as sessões abertas caíram. Gere um novo código para liberar de novo.", "success")
    return redirect(url_for("parceiros_admin.lista", aba="parceiros", editar=pid))


# ───── indicações ─────
@bp_admin.post("/indicacoes/<int:iid>", endpoint="indicacao_editar")
def indicacao_editar(iid):
    i = _ou_404(Indicacao, iid)
    try:
        st = request.form.get("status")
        if st not in STATUS_INDICACAO:
            raise ErroNegocio("Status inválido.")
        valor = _reais_para_cent(request.form.get("valor_setup"))
        if st == "fechada" and not valor:
            raise ErroNegocio("Para marcar como fechada, informe o valor do setup (preço público).")
        i.status, i.valor_setup = st, valor
        if request.form.get("renovar"):
            i.expira_em = agora_utc() + timedelta(days=EXCLUSIVIDADE_DIAS)
        r = db.session.scalar(select(Repasse).where(Repasse.indicacao_id == i.id, Repasse.tipo == "indicacao"))
        if st == "fechada":
            _repasse_unico(i.parceiro_id, "indicacao", valor * COMISSAO_INDICACAO_PCT // 100, indicacao_id=i.id)
        elif r and r.pago_em is None:
            db.session.delete(r)
        elif r:
            flash("Atenção: a comissão desta indicação já foi paga e foi mantida.", "error")
        db.session.commit()
        flash("Indicação atualizada.", "success")
    except ErroNegocio as e:
        db.session.rollback()
        flash(str(e), "error")
    return _volta("indicacoes", status=request.form.get("filtro"))


# ───── pedidos ─────
@bp_admin.post("/pedidos/<int:pid>", endpoint="pedido_editar")
def pedido_editar(pid):
    o = _ou_404(Pedido, pid)
    st = request.form.get("status")
    if st not in STATUS_PEDIDO:
        flash("Status inválido.", "error")
    else:
        o.status = st
        rs = db.session.scalar(select(Repasse).where(Repasse.pedido_id == o.id, Repasse.tipo == "setup"))
        if st in ("pago", "entregue"):
            _repasse_unico(o.parceiro_id, "setup", o.comissao_cent, pedido_id=o.id)
        elif st == "cancelado":
            for r in db.session.scalars(select(Repasse).where(Repasse.pedido_id == o.id, Repasse.pago_em.is_(None))):
                db.session.delete(r)
            if rs and rs.pago_em:
                flash("Atenção: a comissão do setup já foi paga e foi mantida.", "error")
        db.session.commit()
        flash("Pedido atualizado.", "success")
    return _volta("pedidos", status=request.form.get("filtro"))


@bp_admin.post("/pedidos/<int:pid>/mensalidade", endpoint="pedido_mensalidade")
def pedido_mensalidade(pid):
    """Gera o repasse de R$ 30 do mês (depois que o cliente pagou a mensalidade)."""
    o = _ou_404(Pedido, pid)
    ref = (request.form.get("referencia") or _hoje().strftime("%Y-%m")).strip()
    if not o.mensal_cent or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", ref) or o.status not in ("pago", "entregue"):
        flash("Este pedido não tem plano mensal ativo (ou ainda não foi pago).", "error")
    else:
        _, criado = _repasse_unico(o.parceiro_id, "mensal", o.mensal_cent, pedido_id=o.id, referencia=ref)
        db.session.commit()
        flash(f"Mensalidade {ref} lançada." if criado else f"A mensalidade {ref} já estava lançada.", "success")
    return _volta("pedidos", status=request.form.get("filtro"))


# ───── repasses (o que você deve pagar) ─────
@bp_admin.post("/repasses/<int:rid>/pagar", endpoint="repasse_pagar")
def repasse_pagar(rid):
    r = _ou_404(Repasse, rid)
    r.pago_em = None if request.form.get("desfazer") else agora_utc()
    db.session.commit()
    flash("Repasse atualizado.", "success")
    return _volta("repasses", status=request.form.get("filtro") or "a_pagar")


# ═══════════════════════════════════════════════════════════════════════════
def registrar(app):
    app.config.setdefault("PARCEIROS_TENANT", os.environ.get("PARCEIROS_TENANT", "lpparceiros").strip().lower())
    app.add_template_filter(brl, "brl")
    for bp in (bp_parceiros, bp_publico, bp_admin):
        app.register_blueprint(bp)
