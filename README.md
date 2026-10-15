# Webhook Bitrix24 → Linear

O **Bitrix24 é a plataforma principal de gestão das tarefas**. Quando o responsável
muda para **João Cruz (ID 266)**, a integração cria uma issue no Linear para o
desenvolvimento externo, usando os dados do negócio CRM associado à tarefa.

Não existe sincronização de estados, conclusões ou atualizações entre as plataformas.
A conclusão da tarefa no Bitrix24 é feita **internamente, após testes e validação do
desenvolvimento externo**. A integração nunca conclui nem modifica tarefas Bitrix24.

## Configuração do evento

Configure um webhook **de saída** no Bitrix24 para **OnTaskUpdate**, com o endereço
`https://<serviço>/bitrix-task-event`. Guarde o token de aplicação fornecido pelo
Bitrix24 em `BITRIX_OUTGOING_TOKEN`. É diferente do segredo REST de entrada usado em
`BITRIX_WEBHOOK_BASE`.

A rota aceita JSON e `application/x-www-form-urlencoded`, incluindo as chaves
`auth[application_token]` e `data[FIELDS_AFTER][ID]`. Valida o token em tempo constante
antes de consultar serviços externos e ignora outros eventos. Usa apenas o endpoint
REST configurado, nunca URLs ou credenciais recebidas no evento.

O fluxo é:

1. Consultar `tasks.task.get`, selecionando explicitamente `UF_CRM_TASK`, e confirmar
   que o responsável atual é 266.
2. Consultar `tasks.task.history.list`, com paginação, para confirmar uma mudança de
   outro responsável para 266 **no timestamp `ts` do evento**, com precisão de segundos.
   Uma atribuição antiga não transforma uma edição de título, estado ou conclusão
   numa nova atribuição. A criação inicial de uma tarefa já atribuída a 266 é ignorada.
3. Extrair exatamente um negócio `D_<id>` de `ufCrmTask` e consultar `crm.deal.get`.
   Sem negócio ou com vários negócios distintos, devolver o motivo e ignorar a tarefa.
4. Enviar diretamente **uma mutação `issueCreate`**, com título e comentários do
   negócio, fase CRM, valor, moeda, empresa, contacto, responsável do negócio, link do
   negócio e identificação da tarefa. A fase é apenas texto de contexto na descrição;
   não determina o estado da issue. O responsável Linear é opcional.

O histórico deve estar acessível ao utilizador do webhook REST. A comparação usa o
instante do evento, não o da receção, pelo que uma entrega atrasada mantém a referência
original. Se o Bitrix24 apresentar timestamps diferentes entre evento e histórico,
a mudança não é considerada comprovada; validar esta correspondência no portal antes
de ativar. Alterações no mesmo segundo podem partilhar a mesma referência temporal,
mas usam o mesmo ID de issue. Se a tarefa já tiver sido reatribuída a outra pessoa
quando for consultada, o evento é ignorado. `BITRIX_TASK_EVENTS_SINCE` limita a
ativação; eventos anteriores são ignorados.

## UUID determinístico e erros

Não há consultas ao Linear para procurar issues existentes, base de dados local,
ficheiros de estado, reservas, cache ou necessidade de disco persistente.

O ID é calculado a partir de uma tupla JSON com namespace `bitrix-linear-task-v1`,
portal Bitrix24 em minúsculas, equipa Linear normalizada e ID inteiro da tarefa.
Usam-se os primeiros 16 bytes de SHA-256, com os bits de versão/variante ajustados ao
**formato UUID v4 exigido pelo schema Linear**. O valor é determinístico, não aleatório.
Reentregas, pedidos simultâneos, reinícios e novas atribuições da mesma tarefa à mesma
equipa reutilizam o mesmo ID. Outra tarefa, portal ou equipa produz outro ID; mudar
credenciais REST, negócio, título ou descrição não altera a identidade da tarefa.

Isto **reduz o risco de duplicados**, sem garantir processamento exatamente uma vez.
Se o Linear rejeitar um ID existente, a resposta continua a ser um erro 502; a
integração não consulta a issue nem transforma um conflito presumido em sucesso.
Erros HTTP/GraphQL, falhas de transporte e criação não confirmada também são
reportados. Um timeout pode ocorrer após a criação remota: repetir o evento usa o
mesmo ID, mas não confirma o resultado anterior. Não se fazem tentativas com um ID
alternativo. Cada tentativa volta a consultar os dados atuais do negócio.

Um evento inválido devolve 400, token inválido 403, formato não suportado 415,
configuração incompleta 503 e falhas nas APIs 502. Não existe fila autónoma de retries;
monitorização e eventual replay devem ser geridos operacionalmente.

## Rotas existentes e Render Free

`POST /bitrix-linear` mantém a integração existente: cria issues a partir de
`data.FIELDS`, procura o responsável por email quando fornecido, copia anexos do
Bitrix Disk para **Cloudflare R2** e acrescenta os links à descrição. A nova rota de
tarefas não importa anexos do negócio. `GET /healthz` mantém o comportamento existente.

O `render.yaml` mantém **`plan: free`**, sem disco persistente. As novas variáveis são
configuradas no ambiente do serviço, sem guardar segredos no repositório. Este PR não
faz merge, deploy nem alterações no serviço de produção.

## Variáveis de ambiente

| Variável | Uso |
| --- | --- |
| `LINEAR_API_KEY` | Chave de API Linear, sem `Bearer`. |
| `LINEAR_TEAM_ID` | UUID da equipa de destino. |
| `BITRIX_WEBHOOK_BASE` | URL REST autenticada, terminada em `/`; permissões de tarefas, histórico e CRM, além de Disk para a rota original. |
| `BITRIX_OUTGOING_TOKEN` | Obrigatória na nova rota: token de aplicação do webhook de saída. |
| `BITRIX_TASK_EVENTS_SINCE` | Obrigatória na nova rota: data ISO 8601 com fuso, por exemplo `2026-10-15T12:00:00+01:00`; usar o momento real de ativação e manter entre deploys. |
| `LINEAR_JOAO_ASSIGNEE_ID` | Opcional: UUID de João no Linear; se ausente, a issue fica sem responsável. O ID Bitrix24 266 não é um ID Linear. |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ACCOUNT_ID` | Credenciais e conta Cloudflare R2 existentes. |
| `R2_BUCKET_NAME`, `R2_PUBLIC_BASE_URL` | Bucket e URL pública dos anexos existentes. |

## Testes e validação

Requer Python 3.10 ou superior:

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Os testes simulam as APIs e não criam issues reais. Cobrem autenticação, JSON/form,
histórico paginado, mudança efetiva, edição/conclusão sem atribuição, negócios
inválidos/ambíguos, identidade determinística entre tentativas e processos, concorrência,
timeouts e erros Linear sem consultas prévias, além da regressão dos anexos R2.

Antes de ativar, validar num ambiente de teste: mudar uma tarefa de outro utilizador
para 266 (uma issue com os dados CRM), editar o estado ou concluir a tarefa (nenhuma
nova chamada ao Linear), repetir o evento original (mesmo UUID, conflito reportado se
o Linear o rejeitar) e verificar um anexo na rota original. A conclusão funcional
continua a ser feita internamente no Bitrix24 após os testes do desenvolvimento.

Fontes: [OnTaskUpdate](https://apidocs.bitrix24.com/api-reference/tasks/events-tasks/on-task-update.html),
[tasks.task.get](https://apidocs.bitrix24.com/api-reference/tasks/tasks-task-get.html),
[histórico de tarefas](https://apidocs.bitrix24.com/api-reference/tasks/tasks-task-history-list.html),
[schema oficial Linear](https://github.com/linear/linear/blob/master/packages/sdk/src/schema.graphql).
