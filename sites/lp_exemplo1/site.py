"""Manifesto do site lp_exemplo1. SLOTS = imagens que o cliente troca no painel (a tela de upload é GERADA daqui).
No index.html:  {{ img('hero') }}  ou  {{ img_url('hero', w=800) }}
Sem upload do cliente, usa sites/lp_exemplo1/static/<slot>.(webp|png|jpg|svg) ou um placeholder.
Campos do slot: nome, max (maior lado em px), svg (aceita SVG?), dica, formato ("auto"|"png"|"jpg"|"webp").
"""
META = {"nome": "Exemplo 1 — Studio (tema escuro)"}
SLOTS = {
    "logo":     {"nome": "Logo", "max": 1200, "svg": True, "dica": "PNG transparente ou SVG, de preferência para fundo escuro."},
    "hero":     {"nome": "Foto principal (topo)", "max": 2400, "svg": False, "dica": "Foto horizontal, ideal 2200x1100 px."},
    "sobre":    {"nome": "Foto da seção Sobre", "max": 1600, "dica": "Foto vertical ou quadrada, ideal 1200x1400 px."},
    "galeria1": {"nome": "Galeria 1", "max": 1600, "dica": "Ideal 1200x900 px."},
    "galeria2": {"nome": "Galeria 2", "max": 1600, "dica": "Ideal 1200x900 px."},
    "og":       {"nome": "Imagem de compartilhamento", "max": 1200, "formato": "jpg", "dica": "WhatsApp/Facebook/Google. Exatamente 1200x630 px."},
}
