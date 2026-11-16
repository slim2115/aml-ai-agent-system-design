# Process-local settings only; no service starts, downloads or system configuration.
$ErrorActionPreference = 'Stop'
$rebuildRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
if (-not (Test-Path -LiteralPath (Join-Path $rebuildRoot '.rebuild\origin.json'))) { throw 'Missing rebuild provenance.' }
& (Join-Path $PSScriptRoot 'Assert-StandRoot.ps1') $rebuildRoot
[void][IO.Directory]::CreateDirectory((Join-Path $rebuildRoot 'var\tmp'))
$settings = [ordered]@{
    PYTHON_DOTENV_DISABLED='1'; PYTHONDONTWRITEBYTECODE='1'; PYTHONNOUSERSITE='1'
    HF_HUB_OFFLINE='1'; TRANSFORMERS_OFFLINE='1'; HF_HUB_DISABLE_TELEMETRY='1'
    ANONYMIZED_TELEMETRY='FALSE'; OTEL_SDK_DISABLED='true'; LANGSMITH_TRACING='false'; LANGCHAIN_TRACING_V2='false'; DO_NOT_TRACK='1'
    TEMP=(Join-Path $rebuildRoot 'var\tmp'); TMP=(Join-Path $rebuildRoot 'var\tmp')
    HF_HOME=(Join-Path $rebuildRoot 'var\cache\hf'); HF_HUB_CACHE=(Join-Path $rebuildRoot 'var\cache\hf\hub')
    SENTENCE_TRANSFORMERS_HOME=(Join-Path $rebuildRoot 'var\cache\hf\hub')
    TORCH_HOME=(Join-Path $rebuildRoot 'var\cache\torch'); XDG_CACHE_HOME=(Join-Path $rebuildRoot 'var\cache\xdg')
    MPLCONFIGDIR=(Join-Path $rebuildRoot 'var\cache\matplotlib')
    SYNTHETIC_DATA_DIR=(Join-Path $rebuildRoot 'data\synthetic')
    CHROMA_PERSIST_DIR=(Join-Path $rebuildRoot 'var\chroma'); KNOWLEDGE_BASE_COLLECTION='aml_compliance_rules_rebuild'
    OLLAMA_BASE_URL='http://127.0.0.1:11434'; LLM_MODEL_NAME='qwen2.5:14b'
    EMBEDDING_MODEL_NAME='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
    EMBEDDING_MODEL_PATH=(Join-Path $rebuildRoot 'var\models\paraphrase-multilingual-MiniLM-L12-v2')
    EMBEDDING_MANIFEST_PATH=(Join-Path $rebuildRoot 'var\models\paraphrase-multilingual-MiniLM-L12-v2.manifest.json')
    GENERATION_TIMEOUT_SEC='180'; FAITHFULNESS_THRESHOLD='0.90'; SCHEMA_MATCH_THRESHOLD='1.0'
    TRACEABILITY_THRESHOLD='1.0'; REFUSAL_CORRECTNESS_THRESHOLD='0.95'; RAG_RECALL_THRESHOLD='1.0'; RAG_PRECISION_THRESHOLD='1.0'
}
foreach ($entry in $settings.GetEnumerator()) { [Environment]::SetEnvironmentVariable($entry.Key,$entry.Value,'Process') }
foreach ($key in @('PYTHONPATH','PYTHONHOME','DATABASE_URL','SQLALCHEMY_DATABASE_URI')) { [Environment]::SetEnvironmentVariable($key,$null,'Process') }
Set-Location -LiteralPath $rebuildRoot
Write-Output 'Rebuild environment prepared in this PowerShell process. No application or model started.'
