"""Manifesto do site lp_exemplo2 — Ética em Ação (consultoria, tema claro/editorial).
SLOTS = imagens que o cliente troca no painel. Fallbacks (as imagens do protótipo) ficam em static/<slot>.(jpg|png).
Páginas internas: arquivos pagina_<slug>.html nesta pasta (servidos em /<slug>; exige o patch de rota no app.py).
"""
META = {"nome": "Exemplo 2 — Ética em Ação (consultoria, tema claro)"}
SLOTS = {
    "logo":      {"nome": "Logo (pássaro)", "max": 800, "svg": True, "dica": "PNG transparente ou SVG, para fundo claro."},
    "hero":      {"nome": "Foto principal (topo)", "max": 2400, "dica": "Horizontal, ideal 2000x900 px. O texto fica à esquerda: foto com o assunto à direita."},
    "fundo":     {"nome": "Fundo escuro (Por que escolher / Quem somos)", "max": 2000, "dica": "Horizontal, ideal 1800x700 px. Receberá camada escura por cima."},
    "canal":     {"nome": "Foto do Canal de Denúncias", "max": 1400, "dica": "Ideal 900x700 px."},
    "fundadora": {"nome": "Foto da fundadora", "max": 1600, "dica": "Retrato profissional autorizado, ideal 1000x1200 px."},
    "og":        {"nome": "Imagem de compartilhamento", "max": 1200, "formato": "jpg", "dica": "WhatsApp/LinkedIn/Google. Exatamente 1200x630 px."},
    "favicon":   {"nome": "Favicon", "max": 256, "svg": True, "dica": "Quadrado, ideal 256x256 px."},
}
