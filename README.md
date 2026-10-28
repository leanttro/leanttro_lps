# Plataforma multi-tenant de Landing Pages

Um único app/container hospeda as LPs de vários clientes, cada uma no **domínio real do cliente**.
Sem Directus e sem serviço externo de dados: banco SQLite + imagens ficam no volume `/data`.

- **Sites = código seu** (`sites/lp_xxx/`). O cliente só mexe em conteúdo: leads, blog e imagens.
- **Tenant** identificado pelo `Host`. Código do cliente sempre `lp...` (ex.: `lpjulia`).

## Estrutura

```
app.py              TODO o Python (config, models, tenancy, storage, painel, super-admin, CLI)
sites/lp_xxx/       LP (index.html com CSS e JS no próprio arquivo + site.py com os SLOTS de imagem)
templates/          blog padrão, painel/, superadmin/ (cada layout com CSS/JS embutido)
Dockerfile · docker-compose.yml · Caddyfile · .env.example
```

## Decisões tomadas (ambiguidades resolvidas)

1. **Flask-SQLAlchemy** (em vez de sqlite3 puro): o mesmo código roda em SQLite e Postgres só trocando `DATABASE_URL`. SQLite com WAL + `busy_timeout` aguenta bem dezenas de LPs.
2. **Migrações simples**: `db.create_all()` + tabela `schema_info` + lista `MIGRACOES` em `app/dbinit.py` (helper `adicionar_coluna`). O entrypoint roda em todo start.
3. **Rate limit no banco** (tabela `rate_hits`), vale entre workers. Limitação: pode passar 1–2 tentativas do limite sob rajada simultânea. Para tráfego muito alto, troque por Redis.
4. **Cache de tenants em memória, por worker** (TTL 30 s). Editar um tenant limpa o cache do worker que atendeu; os outros expiram em até `TENANT_CACHE_TTL`.
5. **Imagens**: slot aceita PNG/JPG/WEBP (e SVG se `svg: True`). `formato: "auto"` = PNG se houver transparência, senão WEBP. Animações usam só o 1º quadro. Variantes (400/800/1200/1600) são geradas sob demanda em `/data/uploads/<codigo>/.cache/` e servidas em WEBP se o navegador mandar `Accept: image/webp`.
6. **Blog**: corpo em Markdown ou HTML, sanitizado com `nh3`. Imagens só de `/m/<codigo>/` ou `https://`. O que vai ao público é `corpo_html` (já sanitizado).
7. **Impersonação**: link assinado válido por 2 min para `https://<dominio-do-cliente>/painel/acesso`, sessão marcada como "modo suporte".
8. **`/_dominio_ok`**: recusa (404) pedidos que chegam com `X-Forwarded-For` (ou seja, pela internet via proxy). O Caddy consulta direto em `http://app:8000`, sem esse header. Ajuste com `DOMINIO_OK_PUBLICO=1` se usar outro proxy (ver abaixo).
9. **Lead**: honeypot responde sucesso falso sem gravar. Notificação (e-mail/webhook) roda em thread; se falhar, o lead já está salvo e o status aparece no super-admin.

## Rodar local (sem Docker)

```bash
python3.12 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # edite: DEBUG=1, COOKIE_SECURE=0, ALLOW_TENANT_OVERRIDE=1, DATA_DIR=./data
python app.py init-db
python app.py criar-tenant --codigo lpteste --dominio teste.local --template lp_exemplo1 --nome "Teste" --senha "senha12345"
DEBUG=1 gunicorn -c gunicorn.conf.py wsgi:app    # ou: flask --app wsgi run
```

Testar o tenant (escolha uma):

- **/etc/hosts**: adicione `127.0.0.1 teste.local` e abra `http://teste.local:8000`
- **Query/header** (precisa de `DEBUG=1` ou `ALLOW_TENANT_OVERRIDE=1`): `http://localhost:8000/?tenant=lpteste` (a escolha fica em cookie) ou `curl -H "X-Tenant: lpteste" localhost:8000`
- Painel: `/painel` · Super-admin: defina `ADMIN_HOST=localhost`; aí `http://localhost:8000/_admin` (neste caso o `?tenant=` não vale em `localhost`; use `/etc/hosts` para os clientes).

## Subir em produção

1. Aponte o DNS do `ADMIN_HOST` (A) para o IP do servidor. Portas 80 e 443 liberadas.
2. `cp .env.example .env` e preencha **SECRET_KEY**, **ADMIN_HOST**, **ADMIN_USER**, **ACME_EMAIL**.
3. Gere o hash da senha do super-admin e cole no `.env` (aspas simples):
   `docker compose run --rm app python app.py gerar-hash`
4. `docker compose up -d --build`
5. Abra `https://ADMIN_HOST/_admin`.

**Sem o Caddy** (Coolify/EasyPanel/Traefik): suba só o app (`docker compose up -d --build app`, ou use o Dockerfile), monte um volume em `/data`, exponha a porta 8000 e configure o proxy para repassar o `Host` original e `X-Forwarded-*`. O emissor de certificados do seu proxy precisa aceitar domínios dinâmicos; se ele consultar `/_dominio_ok` passando por um proxy interno (com `X-Forwarded-For`), use `DOMINIO_OK_PUBLICO=1`. `PROXY_HOPS` = número de proxies na frente do app.

## Criar um novo cliente (passo a passo)

1. A LP do cliente já existe em `sites/lp_xxx/` (ver abaixo) e foi deployada.
2. **Super-admin → Novo cliente** (ou CLI):
   ```bash
   docker compose exec app python app.py criar-tenant \
     --codigo lpjulia --dominio julia.com.br --dominio www.julia.com.br \
     --template lp_julia --nome "Julia Theila" --senha "SenhaInicial123" --whatsapp 11999999999 --email julia@gmail.com
   ```
3. Passe ao cliente: o endereço `https://julia.com.br/painel` e a senha.
4. O cliente só precisa **apontar o DNS** (próxima seção). O certificado HTTPS é emitido sozinho no primeiro acesso.

## Criar uma nova LP

1. `cp -r sites/lp_exemplo1 sites/lp_julia` (pasta **obrigatoriamente** `lp_` + letras/números/_).
2. Edite `index.html` (Jinja + Tailwind) e `site.py` (`SLOTS` = imagens que o cliente troca; `META["nome"]` aparece na lista do super-admin).
3. No HTML use: `{{ img('hero') }}`, `{{ img_url('hero', w=800) }}`, `{{ img_srcset('hero') }}`, `{{ tem_img('logo') }}`, `{{ posts_recentes }}`, `{{ whatsapp_link('texto') }}`, `{{ capa_url(post, 800) }}`, `{{ site_static('arquivo.css') }}`. Imagens padrão (fallback) vão em `sites/lp_julia/static/<slot>.webp|png|jpg|svg`.
4. Formulário: `<form data-lead action="/lead" method="post">` + `<script src="{{ url_for('static', filename='lead.js') }}" defer></script>`. Campos conhecidos: `nome`, `telefone`/`whatsapp`, `email`, `mensagem`; qualquer outro campo vira "extras" (JSON). Mantenha o campo honeypot `website` escondido.
5. Opcionais na pasta do site: `blog_index.html`, `blog_post.html`, `obrigado.html`, `404.html` (sobrescrevem os padrões; podem estender `_blog_layout.html`).
6. Faça deploy (`git pull && docker compose up -d --build`) e escolha o template no cliente. Super-admin → **Limpar cache** relê os `site.py`.

Como o código é seu, o painel do cliente **nunca** altera `sites/`.

## DNS do cliente

O cliente (ou você) só cria um destes registros no domínio dele:

- `A` → IP do seu servidor (para `julia.com.br` e, se quiser, `www`), ou
- `CNAME` de `www` → um hostname seu que aponte para o servidor (ex.: `lp.seudominio.com.br`).

`www.` e domínio raiz valem para o mesmo cliente (cadastre os dois ou só um; o `www` é normalizado). Cloudflare: use "DNS only" (nuvem cinza) ou ajuste `PROXY_HOPS=2`.

## Endpoints úteis

`/` LP · `/blog` · `/blog/<slug>` · `/blog/feed.xml` · `/sitemap.xml` · `/robots.txt` · `POST /lead` · `POST /api/lead` (JSON) · `/obrigado` · `/m/<codigo>/<arquivo>[?w=800&v=..]` · `/painel` · `/healthz` · `/_dominio_ok?domain=x` · `/_admin` (só no ADMIN_HOST).

## Backup e restore do /data

```bash
# Backup (banco + imagens). Para o SQLite consistente, use o backup do próprio sqlite:
docker compose exec app python -c "import sqlite3; s=sqlite3.connect('/data/db.sqlite'); d=sqlite3.connect('/data/backup.sqlite'); s.backup(d)"
docker run --rm -v "$(basename "$PWD")_app_data":/data -v "$PWD":/bkp alpine tar czf /bkp/data-$(date +%F).tar.gz -C /data .

# Restore (com o app parado)
docker compose down
docker run --rm -v "$(basename "$PWD")_app_data":/data -v "$PWD":/bkp alpine sh -c "rm -rf /data/* && tar xzf /bkp/data-AAAA-MM-DD.tar.gz -C /data && chown -R 1000:1000 /data"
docker compose up -d
```
(O nome do volume é `<pasta-do-projeto>_app_data`; confirme com `docker volume ls`. Guarde também o `.env`, em especial `SECRET_KEY`.) Salve também o volume `caddy_data` (certificados).

## Atualizar sem perder dados

```bash
git pull
docker compose up -d --build     # volumes (/data) são preservados; o entrypoint roda init-db (idempotente)
```
Nunca use `docker compose down -v` (apaga os volumes).

## Segurança (resumo)

CSRF por sessão em todo POST do painel/super-admin · cookies HttpOnly + SameSite=Lax + Secure · `SECRET_KEY` obrigatória (falha ao subir sem ela) · sessão do painel presa ao tenant do Host e invalidada ao trocar senha · toda query de leads/posts filtra por `tenant_id` · HTML do blog sanitizado (nh3) · SVG validado e servido com CSP sandbox · `MAX_CONTENT_LENGTH` · CSV sem injeção de fórmula · logs do gunicorn sem query string.

## Checklist de testes manuais

Use dois clientes: `lpa` (a.local) e `lpb` (b.local), ambos no `/etc/hosts`.

1. **Isolamento de dados**: logue em `a.local/painel`, crie post e lead. Em `b.local/painel` nada disso aparece. Em `b.local/blog/<slug-do-post-de-A>` → 404.
2. **Sessão por domínio**: logado em `a.local/painel`, abra `b.local/painel` → pede login.
3. **Isolamento de mídia**: envie `hero` em A. `http://b.local:8000/m/lpa/hero.webp` → 404. Em A → 200.
4. **Path traversal**: `/m/lpa/..%2f..%2fdb.sqlite`, `/m/lpa/../lpb/hero.webp`, `/m/../etc/passwd`, `/s/..%2f..%2fsite.py` → 404.
5. **Upload inválido**: envie um `.txt` renomeado para `.jpg`, um arquivo vazio, uma imagem >15 MB, um `.gif`, um SVG num slot sem `svg:True`, um SVG com `<script>` → mensagem clara em português em cada caso.
6. **Volume**: rode sem montar `/data` (ex.: `docker run` sem `-v`) → aviso amarelo no painel; com `/data` somente leitura → aviso vermelho/erro de permissão.
7. **Domínio desconhecido**: `curl -i -H "Host: naoexiste.com" localhost:8000/` → 404 limpo (sem detalhes). `curl "localhost:8000/_dominio_ok?domain=a.local"` → 200; `?domain=naoexiste.com` → 404; domínio de tenant desativado → 404; com header `X-Forwarded-For: 1.2.3.4` → 404.
8. **Tenant desativado**: desative A no super-admin → `a.local` passa a dar 404.
9. **Lead**: envie o formulário da LP (JSON via lead.js e também sem JS → redirect `/obrigado`). Confirme UTMs (`/?utm_source=x`), extras (campo `interesse`), honeypot (preencha `website` → 200 mas nada gravado), telefone/e-mail inválido (422), 6ª tentativa em 10 min (429), `curl` sem UA de navegador (403).
10. **Falha de notificação**: configure um webhook inválido e SMTP errado → lead continua gravado e a resposta é 200; o super-admin mostra `erro:...` em "Aviso".
11. **CSRF**: `curl -X POST a.local/painel/leads/1/atendido` (sem token) → 400.
12. **Rate limit de login**: 7 senhas erradas seguidas → 429.
13. **Super-admin**: `https://ADMIN_HOST/_admin` pede login; num domínio de cliente `/_admin` → 404; criar/editar/desativar/resetar senha/excluir (exige digitar o código); "Entrar no painel do cliente" abre com faixa de modo suporte e o link expira em 2 min; baixar CSV.
14. **Blog**: rascunho não aparece em `/blog`, sitemap nem feed; publicado aparece; `<script>` colado no corpo HTML é removido; imagem no corpo; `/blog/feed.xml` e `/sitemap.xml` válidos com o domínio do cliente.
15. **Imagens**: trocar/remover slot (volta ao padrão em `sites/.../static`), galeria livre + "Copiar URL", PNG transparente no logo continua transparente, `?w=800` gera `.cache/`.
16. **HTTPS**: com DNS apontado, acessar `https://julia.com.br` emite o certificado na hora; domínio não cadastrado apontando para o servidor → handshake recusado (sem certificado).
