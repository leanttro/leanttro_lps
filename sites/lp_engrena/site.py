"""Manifesto do site lp_engrena — 1ª Feira de Rua Engrena Meu Bairro.
Logo e demais imagens estão no Directus (URL no HTML). Slots abaixo: fundo e og.
"""
META = {"nome": "Engrena Meu Bairro — 1ª Feira de Rua"}
SLOTS = {
    "fundo": {"nome": "Imagem de fundo (hero e seções)", "max": 2400, "svg": False, "formato": "webp", "dica": "Textura/ilustração de fundo. Ideal 2000 px de largura."},
    "og":    {"nome": "Imagem de compartilhamento", "max": 1200, "formato": "jpg", "dica": "WhatsApp/Facebook/Google. Exatamente 1200x630 px."},
}
