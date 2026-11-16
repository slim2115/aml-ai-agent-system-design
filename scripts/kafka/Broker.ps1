#Requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Validate','Start','Stop','Status','Topics','Lag','ReadDlq','PublishInvalid')]
    [string] $Action,
    [ValidateSet('Bookkeeping','AML')]
    [string] $Flow = 'Bookkeeping'
)

$ErrorActionPreference = 'Stop'
$kafkaRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
& (Join-Path $PSScriptRoot '..\rebuild\Assert-StandRoot.ps1') $kafkaRoot
$kafkaCompose = Join-Path $kafkaRoot 'lab\kafka\compose.yaml'
$kafkaOwner = 'aml-ai-agent-system-rebuild:kafka:v1'
$kafkaTopics = @('aml.lab.incident-review.v1','aml.lab.incident-review.dlq.v1')
$kafkaGroup = 'aml-lab-review-v1'
if ($Flow -eq 'AML') {
    $kafkaTopics = @('aml.lab.analysis-requested.v1','aml.lab.analysis-requested.dlq.v1')
    $kafkaGroup = 'aml-lab-analysis-v1'
}
$kafkaDocker = Get-Command docker -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $kafkaDocker) {
    # A terminal opened before installation may still have the previous PATH.
    foreach ($kafkaDockerPath in @((Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'), 'C:\Program Files\Docker\Docker\resources\bin\docker.exe')) {
        if (Test-Path -LiteralPath $kafkaDockerPath) {
            $kafkaDocker=Get-Command $kafkaDockerPath -CommandType Application
            break
        }
    }
}
if (-not $kafkaDocker) {
    throw 'Docker CLI not found. Install Docker Desktop with WSL 2, start its Linux engine, reopen the terminal, and follow docs/operator/kafka-quickstart.md. This script does not install software.'
}
$kafkaComposeArgs = @('compose','--project-name','aml-kafka-lab','--project-directory',$kafkaRoot,'--file',$kafkaCompose)

function Invoke-KafkaDocker {
    param([string[]] $DockerArgs, [int] $TimeoutSeconds = 45, [AllowNull()][string] $InputText = $null)
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $kafkaDocker.Source
    $start.WorkingDirectory = $kafkaRoot
    $start.UseShellExecute = $false
    # Docker invokes sibling credential helpers by name, even when the CLI was
    # located by its full path in a terminal opened before installation.
    $start.Environment['PATH'] = [IO.Path]::GetDirectoryName($kafkaDocker.Source) + [IO.Path]::PathSeparator + $start.Environment['PATH']
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.RedirectStandardInput = $true
    $start.StandardInputEncoding = [Text.UTF8Encoding]::new($false)
    foreach ($item in $DockerArgs) { [void] $start.ArgumentList.Add($item) }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw 'Could not start Docker CLI.' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if ($null -ne $InputText) { $process.StandardInput.Write($InputText) }
        $process.StandardInput.Close()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $process.Kill($true)
            [void] $process.WaitForExit(5000)
            throw "Docker command exceeded $TimeoutSeconds seconds. Containers may remain running; use -Action Status to inspect them."
        }
        $outText = $stdout.GetAwaiter().GetResult()
        $errText = $stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            throw "Docker command failed (exit $($process.ExitCode)). $errText $outText"
        }
        if ($errText.Trim()) { Write-Host $errText.Trim() }
        return $outText.Trim()
    } finally { $process.Dispose() }
}

function Invoke-KafkaCli {
    param([string] $Tool, [string[]] $ToolArgs, [int] $TimeoutSeconds = 45, [AllowNull()][string] $InputText = $null)
    Invoke-KafkaDocker -DockerArgs ($kafkaComposeArgs + @('exec','-T','-e','KAFKA_HEAP_OPTS=-Xms64m -Xmx256m','kafka',"/opt/kafka/bin/$Tool") + $ToolArgs) -TimeoutSeconds $TimeoutSeconds -InputText $InputText
}

function Assert-KafkaOwnership {
    $ids = Invoke-KafkaDocker -DockerArgs @('ps','-aq','--filter','label=com.docker.compose.project=aml-kafka-lab')
    if ($ids) {
        $containers = Invoke-KafkaDocker -DockerArgs (@('inspect') + @($ids -split '\s+')) | ConvertFrom-Json
        foreach ($container in $containers) {
            if ($container.Config.Labels.'aml.lab.owner' -ne $kafkaOwner -or
                $container.Config.Labels.'com.docker.compose.project.config_files' -ne $kafkaCompose) {
                throw 'The Docker project name belongs to a different checkout/container. No container was changed.'
            }
        }
    }
    $volumes = Invoke-KafkaDocker -DockerArgs @('volume','ls','--quiet','--filter','name=^aml-kafka-lab_kafka-data$')
    if ($volumes) {
        $volume = @(Invoke-KafkaDocker -DockerArgs @('volume','inspect','aml-kafka-lab_kafka-data') | ConvertFrom-Json)[0]
        if ($volume.Labels.'aml.lab.owner' -ne $kafkaOwner) {
            throw 'The Kafka volume has another owner. No volume was changed.'
        }
    }
}

function Get-KafkaTopicDescription {
    param([string] $Topic)
    Invoke-KafkaCli -Tool 'kafka-topics.sh' -ToolArgs @('--bootstrap-server','kafka:19092','--describe','--topic',$Topic)
}

function Assert-KafkaTopic {
    param([string] $Topic)
    $description = Get-KafkaTopicDescription -Topic $Topic
    if ($description -notmatch 'PartitionCount:\s*3\s' -or $description -notmatch 'ReplicationFactor:\s*1\s') {
        throw "Topic $Topic has unexpected partitions/replication. Existing topology is preserved; inspect -Action Topics."
    }
    foreach ($setting in @('retention.ms=604800000','retention.bytes=134217728','segment.bytes=16777216','cleanup.policy=delete')) {
        if ($description -notmatch ('(?:[\s,]|^)' + [regex]::Escape($setting) + '(?:[,\s]|$)')) {
            throw "Topic $Topic differs from the lab configuration ($setting). Existing settings are preserved."
        }
    }
    return $description
}

# Local host clients cannot use a remote engine's published loopback port.
$kafkaContextArgs=@('context','inspect')
if ($env:DOCKER_CONTEXT) { $kafkaContextArgs+=$env:DOCKER_CONTEXT }
$contexts = @(Invoke-KafkaDocker -DockerArgs $kafkaContextArgs -TimeoutSeconds 15 | ConvertFrom-Json)
# DOCKER_CONTEXT takes precedence over DOCKER_HOST in the Docker CLI.
$kafkaEndpoint=if ($env:DOCKER_CONTEXT) { $contexts[0].Endpoints.docker.Host }
    elseif ($env:DOCKER_HOST) { $env:DOCKER_HOST }
    else { $contexts[0].Endpoints.docker.Host }
if (-not $kafkaEndpoint -or $kafkaEndpoint.Replace('\','/') -notmatch '^npipe:/+\./pipe/(docker_engine|dockerDesktopLinuxEngine)$') {
    throw 'Use the local Docker Desktop Linux engine. Remote contexts and remote named pipes are not supported.'
}
[void] (Invoke-KafkaDocker -DockerArgs @('compose','version') -TimeoutSeconds 15)
[void] (Invoke-KafkaDocker -DockerArgs ($kafkaComposeArgs + @('config','--quiet')) -TimeoutSeconds 15)
if ($Action -eq 'Validate') {
    Write-Output 'Compose syntax and local Docker context checked. Engine/topic readiness was not checked.'
    return
}
try { $engine = Invoke-KafkaDocker -DockerArgs @('info','--format','{{.OSType}}') -TimeoutSeconds 15 }
catch { throw "Docker engine is unavailable. Start Docker Desktop and wait for its Linux engine. $($_.Exception.Message)" }
if ($engine -ne 'linux') { throw 'Switch Docker Desktop to Linux containers.' }
Assert-KafkaOwnership

switch ($Action) {
    'Start' {
        Write-Host 'Starting the local Kafka broker; the first image download may take several minutes.'
        Invoke-KafkaDocker -DockerArgs ($kafkaComposeArgs + @('up','-d','--wait','--wait-timeout','120','kafka')) -TimeoutSeconds 300
        foreach ($topic in $kafkaTopics) {
            Invoke-KafkaCli -Tool 'kafka-topics.sh' -ToolArgs @('--bootstrap-server','kafka:19092','--create','--if-not-exists','--topic',$topic,'--partitions','3','--replication-factor','1','--config','retention.ms=604800000','--config','retention.bytes=134217728','--config','segment.bytes=16777216','--config','cleanup.policy=delete')
            Assert-KafkaTopic -Topic $topic
        }
        Write-Output 'Kafka ready at 127.0.0.1:9092. PostgreSQL, API and the worker must be started separately.'
    }
    'Stop' {
        Write-Output 'Stopping the shared broker affects both bookkeeping and AML flows.'
        Invoke-KafkaDocker -DockerArgs ($kafkaComposeArgs + @('stop','--timeout','30','kafka')) -TimeoutSeconds 45
        Write-Output 'Kafka stopped. Containers, topics and the named data volume are retained.'
    }
    'Status' { Invoke-KafkaDocker -DockerArgs ($kafkaComposeArgs + @('ps','--all')) }
    'Topics' { foreach ($topic in $kafkaTopics) { Get-KafkaTopicDescription -Topic $topic } }
    'Lag' {
        Invoke-KafkaCli -Tool 'kafka-consumer-groups.sh' -ToolArgs @('--bootstrap-server','kafka:19092','--describe','--group',$kafkaGroup,'--timeout','10000')
    }
    'ReadDlq' {
        # Explicit partition assignment avoids joining or moving the worker's group.
        foreach ($partition in 0..2) {
            Write-Host "DLQ partition $partition (up to 20 messages, 3-second idle timeout):"
            # kafka-console-consumer exits nonzero on its idle timeout; report it as a bounded read.
            try {
                Invoke-KafkaCli -Tool 'kafka-console-consumer.sh' -ToolArgs @('--bootstrap-server','kafka:19092','--topic',$kafkaTopics[1],'--partition',"$partition",'--offset','earliest','--max-messages','20','--timeout-ms','3000','--consumer-property','enable.auto.commit=false','--property','print.partition=true','--property','print.offset=true','--property','print.key=true') -TimeoutSeconds 20
            } catch {
                if ($_.Exception.Message -match 'org\.apache\.kafka\.common\.errors\.TimeoutException') { Write-Output $_.Exception.Message }
                else { throw }
            }
        }
    }
    'PublishInvalid' {
        $poison = 'INC-000123|{"synthetic":true,"exercise":"invalid-event-missing-required-fields"}' + "`n"
        Invoke-KafkaCli -Tool 'kafka-console-producer.sh' -ToolArgs @('--bootstrap-server','kafka:19092','--topic',$kafkaTopics[0],'--sync','--property','parse.key=true','--property','key.separator=|','--producer-property','acks=all','--producer-property','delivery.timeout.ms=10000','--producer-property','request.timeout.ms=5000','--producer-property','max.block.ms=10000') -TimeoutSeconds 20 -InputText $poison
        Write-Output 'One predefined synthetic invalid event was submitted. Inspect the worker and -Action ReadDlq.'
    }
}
