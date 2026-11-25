"""Manifesto do site lp_categorias — Seu Vizinho (LP institucional que liga Loja, Portal e Artesanato na Rede).
Todas as imagens são slots (upload). Sem upload, a página usa a foto de referência original.
"""
META = {"nome": "Seu Vizinho — Conectando pessoas, ideias e oportunidades"}

def _s(nome, max_, fmt, dica):
    return {"nome": nome, "max": max_, "formato": fmt, "dica": dica}

SLOTS = {
    "og": _s("Imagem de compartilhamento", 1200, "jpg", "WhatsApp/Facebook/Google. Exatamente 1200x630 px."),
    "logo": _s("Logo (ícone do topo)", 300, "png", "PNG com fundo transparente, ~68x62 px (envie 2x). Substitui o ícone da casinha."),
    "hero": _s("Hero — foto principal", 1400, "jpg", "Foto à direita do título. Horizontal, ~1400x470 px."),
    "card_loja": _s("Card Loja Seu Vizinho — fundo", 900, "jpg", "Fundo do card verde. ~900x400 px, assunto à direita."),
    "card_portal": _s("Card Portal Seu Vizinho — fundo", 900, "jpg", "Fundo do card azul. ~900x400 px, assunto à direita."),
    "card_artesanato": _s("Card Artesanato na Rede — fundo", 900, "jpg", "Fundo do card rosa. ~900x400 px, assunto à direita."),
    "comunidade_1": _s("Comunidade — foto 1", 500, "jpg", "Estilo polaroide, quase quadrada (~500x600)."),
    "comunidade_2": _s("Comunidade — foto 2", 500, "jpg", "Estilo polaroide, quase quadrada (~500x600)."),
    "comunidade_3": _s("Comunidade — foto 3", 500, "jpg", "Estilo polaroide, quase quadrada (~500x600)."),
    "comunidade_4": _s("Comunidade — foto 4", 500, "jpg", "Estilo polaroide, quase quadrada (~500x600)."),
    "destaque_1": _s("Em Destaque — card 1 (Aulas)", 600, "jpg", "~600x400 px."),
    "destaque_2": _s("Em Destaque — card 2 (Aulas)", 600, "jpg", "~600x400 px."),
    "destaque_3": _s("Em Destaque — card 3 (Dicas)", 600, "jpg", "~600x400 px."),
    "destaque_4": _s("Em Destaque — card 4 (Comunidade)", 600, "jpg", "~600x400 px."),
    "cta": _s("Faixa final — fundo", 1600, "jpg", "Fundo da faixa azul 'Seu Vizinho é mais que um site'. ~1600x400 px, fica escurecida."),
}
