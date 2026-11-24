"""
Educa-Med — Camada de Aplicação (rotas da API).

Fluxo de uma mensagem no /chat:
    1. valida a mensagem               (services/seguranca.py)
    2. bloqueia casos clínicos reais   (services/seguranca.py)
    3. pergunta para a IA              (services/ia_service.py)
    4. atualiza perfil e cronograma    (services/perfil.py, services/cronograma.py)
    5. registra a resposta para checagem do aluno (services/respostas.py)
"""

from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from services import cronograma, respostas, seguranca
from services.arquivos import limpar_todos_os_dados
from services.ia_service import gerar_material_estudo, gerar_resposta_ia
from services.perfil import atualizar_perfil, carregar_perfil

load_dotenv()  # lê a GROQ_API_KEY do arquivo .env

PASTA_FRONTEND = Path(__file__).resolve().parent / "frontend"

app = FastAPI(title="Educa-Med — Assistente de Estudos para Medicina")
app.mount("/static", StaticFiles(directory=PASTA_FRONTEND), name="static")


# ---------- Modelos (formato dos dados que chegam do front-end) ----------
class Mensagem(BaseModel):
    message: str


# Field(max_length=...) impede textos gigantes; o FastAPI responde 422 sozinho se passar do limite
class NovaTarefa(BaseModel):
    dia: str = Field(max_length=30)
    atividade: str = Field(max_length=200)
    duracao: str = Field(max_length=30)


class EdicaoTarefa(BaseModel):
    dia: str | None = Field(default=None, max_length=30)
    atividade: str | None = Field(default=None, max_length=200)
    duracao: str | None = Field(default=None, max_length=30)
    concluida: bool | None = None


class Checagem(BaseModel):
    status: Literal["pendente", "conferida", "com_erro"]


# ---------- Interface ----------
@app.get("/")
def pagina_inicial():
    return FileResponse(PASTA_FRONTEND / "index.html")


# ---------- Chat com a IA ----------
@app.post("/chat")
def chat(pedido: Mensagem):
    mensagem = pedido.message.strip()

    erro = seguranca.validar_mensagem(mensagem)
    if erro:
        raise HTTPException(status_code=400, detail=erro)

    if seguranca.e_pedido_clinico(mensagem):
        return {"mensagem": seguranca.RESPOSTA_BLOQUEIO, "bloqueada": True}

    try:
        dados = gerar_resposta_ia(mensagem, carregar_perfil(), cronograma.listar_tarefas())
    except RuntimeError as erro_ia:
        raise HTTPException(status_code=503, detail=str(erro_ia))

    perfil = atualizar_perfil(dados)

    if len(dados["cronograma"]) > 0:
        cronograma.substituir_tarefas_da_ia(dados["cronograma"])

    registro = respostas.registrar_resposta(
        mensagem, dados["mensagem"], dados["onde_conferir"], dados["material"]
    )

    return {
        "mensagem": dados["mensagem"],
        "material": dados["material"],
        "onde_conferir": dados["onde_conferir"],
        "id_resposta": registro["id"],
        "area_atual": perfil["area"],
        "cronograma_atualizado": len(dados["cronograma"]) > 0,
        "bloqueada": False,
    }


# ---------- CRUD do cronograma (o artefato) ----------
@app.get("/tarefas")
def listar_tarefas():
    tarefas = cronograma.listar_tarefas()
    return {
        "area": carregar_perfil()["area"],
        "tarefas": tarefas,
        "resumo": cronograma.montar_resumo(tarefas),
    }


@app.post("/tarefas", status_code=201)
def criar_tarefa(nova: NovaTarefa):
    if not nova.atividade.strip():
        raise HTTPException(status_code=400, detail="Descreva a atividade.")
    if not nova.duracao.strip():
        raise HTTPException(status_code=400, detail="Informe a duração (ex: 1h30).")
    return cronograma.criar_tarefa(nova.dia, nova.atividade, nova.duracao)


@app.get("/tarefas/{id_tarefa}")
def buscar_tarefa(id_tarefa: int):
    try:
        return cronograma.buscar_tarefa(id_tarefa)
    except KeyError as erro:
        raise HTTPException(status_code=404, detail=erro.args[0])


@app.put("/tarefas/{id_tarefa}")
def atualizar_tarefa(id_tarefa: int, edicao: EdicaoTarefa):
    try:
        return cronograma.atualizar_tarefa(id_tarefa, edicao.model_dump())
    except KeyError as erro:
        raise HTTPException(status_code=404, detail=erro.args[0])


@app.delete("/tarefas/{id_tarefa}", status_code=204)
def remover_tarefa(id_tarefa: int):
    try:
        cronograma.remover_tarefa(id_tarefa)
    except KeyError as erro:
        raise HTTPException(status_code=404, detail=erro.args[0])


# ---------- Estudar uma tarefa: resumo + questões geradas pela IA ----------
@app.post("/tarefas/{id_tarefa}/estudar")
def estudar_tarefa(id_tarefa: int):
    try:
        tarefa = cronograma.buscar_tarefa(id_tarefa)
    except KeyError as erro:
        raise HTTPException(status_code=404, detail=erro.args[0])

    try:
        material = gerar_material_estudo(tarefa["atividade"], carregar_perfil())
    except RuntimeError as erro_ia:
        raise HTTPException(status_code=503, detail=str(erro_ia))

    registro = respostas.registrar_resposta(
        f"Estudar: {tarefa['atividade']}", material["resumo"], material["onde_conferir"], material
    )
    return {"id_resposta": registro["id"], "atividade": tarefa["atividade"], **material}


# ---------- Checagem das respostas da IA (letramento em IA) ----------
@app.get("/respostas")
def listar_respostas():
    lista = respostas.listar_respostas()
    return {"respostas": lista, "senso_critico": respostas.calcular_senso_critico(lista)}


@app.put("/respostas/{id_resposta}")
def checar_resposta(id_resposta: int, checagem: Checagem):
    try:
        return respostas.atualizar_status(id_resposta, checagem.status)
    except ValueError as erro:
        raise HTTPException(status_code=400, detail=str(erro))
    except KeyError as erro:
        raise HTTPException(status_code=404, detail=erro.args[0])


# ---------- Recomeçar ----------
@app.delete("/sessao", status_code=204)
def recomecar():
    limpar_todos_os_dados()

    # Meu primeiro teste com Git e GitHub
