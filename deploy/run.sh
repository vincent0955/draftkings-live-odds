#!/bin/bash
# usage: bash run.sh script.sh   -> runs it on the odds server via SSM and prints the output
python3 -c "import json,sys; print(json.dumps({'commands':[open(sys.argv[1]).read()]}))" "$1" > /tmp/params.json
CMD=$(aws ssm send-command --instance-ids i-02f53b11713c3d308 --document-name AWS-RunShellScript --parameters file:///tmp/params.json --query Command.CommandId --output text)
for i in $(seq 1 40); do sleep 3; S=$(aws ssm get-command-invocation --command-id $CMD --instance-id i-02f53b11713c3d308 --query Status --output text 2>/dev/null); case $S in Success|Failed|Cancelled|TimedOut) break;; esac; done
aws ssm get-command-invocation --command-id $CMD --instance-id i-02f53b11713c3d308 --query '[Status,StandardOutputContent,StandardErrorContent]' --output text
