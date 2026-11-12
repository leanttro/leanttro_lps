"""Manifesto do site lp_inovare — INOVARE Etiquetas e Brindes.
Catálogo (categorias e produtos) e blog são cadastrados no painel do cliente (abas Categorias, Produtos e Blog).
Slots abaixo: imagens fixas que o cliente troca em "Imagens do site".
"""
META = {"nome": "INOVARE — Etiquetas e Brindes",
        "icones": ["etiqueta", "presente", "camiseta", "adesivo", "estrela", "escudo", "paleta", "caixa"]}
SLOTS = {
    "hero": {"nome": "Imagem principal (topo da home)", "max": 1600, "svg": False, "dica": "PNG transparente ou foto com os produtos. Ideal 1200x900 px."},
    "og":   {"nome": "Imagem de compartilhamento", "max": 1200, "formato": "jpg", "dica": "WhatsApp/Facebook/Google. Exatamente 1200x630 px."},
}
