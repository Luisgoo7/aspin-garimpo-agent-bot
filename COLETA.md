# Estratégia de coleta ASPIN

## Ordem de prioridade

1. **nanojud** (`lucmolero/nanojud`, MIT): primeira camada para descoberta de novos candidatos, especialmente eSAJ/TJSP e DJEN, além de normalização/consulta DataJud.
2. **pydatajud** (`Filipe-Araujo0/pydatajud`, Unlicense): enriquecimento de processos conhecidos, consulta por número CNJ e extração estruturada de movimentos.
3. **datajud-cli** (`cleisabino/datajud-cli`, MIT): operação em lote, cache local e exportação JSON/CSV para reprocessamento e auditoria.

## O que realmente encontra novos ofícios

- **DJEN/eSAJ/TJSP**: podem fornecer publicações ou documentos candidatos que não dependem de já conhecer o número do processo.
- **DataJud**: fornece metadados e movimentações públicas, útil para localizar eventos e confirmar processos; não deve ser tratado como repositório garantido do PDF integral do ofício.
- **IA**: classifica somente candidatos encontrados pelas fontes, preservando URL, tribunal, data e trecho de evidência.

## Fluxo planejado

```text
nanojud (descoberta) -> normalização -> DataJud/pydatajud (enriquecimento)
      -> datajud-cli (lote/exportação) -> regras -> Groq/NVIDIA -> Supabase -> Telegram
```

Os pacotes são dependências auxiliares. A consulta direta DataJud existente continua como fallback operacional enquanto os adaptadores nanojud/eSAJ/DJEN são validados com processos públicos de teste. Não haverá bypass de captcha, autenticação, segredo de justiça ou restrições do tribunal.

## Limitação comercial

Antes de transformar a coleta em produto comercial, validar os termos de uso de cada fonte, especialmente o DataJud, e consultar a fonte oficial quando a informação for usada em atividade profissional.
