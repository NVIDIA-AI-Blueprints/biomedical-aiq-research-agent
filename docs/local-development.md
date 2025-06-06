# Local Development Guide

## Getting Started

To run locally, start by [installing the uv python package and project manager](https://docs.astral.sh/uv/getting-started/installation/). 

Next create a virtual environment using Python 3.12:

```bash
uv python install 3.12
uv venv --python 3.12 --python-preference managed
uv pip install -e "./aira[dev]"
```

Spin up the BioNeMo NIMs locally if you would like to host them locally (otherwise use the NVIDIA AI Endpoint):
```bash
docker compose -f deploy/compose/docker-compose.yaml --profile deploy-bionemo-nims-locally up -d
```

Update the configuration file located at `aira/configs/config.yaml`, providing values for a RAG deployment and your reasoning and instruct LLMs. The configuration file includes comments on what values to update.

Run the backend service:

```bash
# optionally export the Tavily search key
export TAVILY_API_KEY=your-tavily-api-key
# export your NVIDIA API KEY
export NVIDIA_API_KEY=your-nvidia-api-key

# set to true if you want to use the publicly hosted NIMs on the NVIDIA AI Endpoints for the LLM NIMs
# set to false if locally deploying the LLM NIMs
export AIRA_HOSTED_NIMS=true

# Specify the MolMIM and DiffDock URLs
# If these two NIMs are locally deployed via "docker compose -f deploy/compose/docker-compose.yaml --profile deploy-bionemo-nims-locally up -d":
export MOLMIM_ENDPOINT_URL=http://bionemo-molmim-nim:8000/generate
export DIFFDOCK_ENDPOINT_URL=http://bionemo-diffdock-nim:8000/molecular-docking/diffdock/generate
# If these two NIMs are not locally deployed and we want to call the public NVIDIA AI Endpoint:
export MOLMIM_ENDPOINT_URL=https://health.api.nvidia.com/v1/biology/nvidia/molmim/generate
export DIFFDOCK_ENDPOINT_URL=https://health.api.nvidia.com/v1/biology/mit/diffdock

# run the service
uv run aiq serve --config_file aira/configs/config.yml --host 0.0.0.0 --port 3838
```

You can now access the backend at `http://localhost:3838/docs`. 

### Test with the AIRA demo web application

To test your custom backend against the pre-built AIRA demo web application, you need to use Docker to run the nginx proxy and frontend.

1. Run the nginx proxy that sits between the frontend, backend, and RAG. Update the value `UPDATE-TO-YOUR-RAG-SERVER-IP` below:

```bash
docker run \
  -v $(pwd)/deploy/compose/nginx.conf.template:/etc/nginx/templates/nginx.conf.template \
  -e RAG_INGEST_URL=http://UPDATE-TO-YOUR-RAG-SERVER-IP:8082 \
  -e AIRA_BASE_URL=http://localhost:3838 \
  --network host \
  nginx:latest
```

> Tip: Local development requires the Docker network host. If you are using Docker for Desktop, ensure you have enabled the network host under Settings -> Network

2. Run the AIRA frontend 

```bash
docker run \
  -e INFERENCE_ORIGIN=http://localhost:8051 \
  nvcr.io/nvidia/blueprint/aira-frontend:v1.0.0
```

## Unit Tests

To run the developer unit tests, follow the instructions in `test_aira/README.md`

## Developer Architecture

One of the main benefits of the Biomedical AI-Q Research Agent is the ability to do human-in-the-loop intervention in the deep research process, and to do so at scale via a stateless REST interface. This capability is achieved by breaking the deep research process into 3 distinct steps:

1. `generate_queries` - takes the user's desired report structure and asks the reasoning model to create relevant research queries 
2. `generate_summary` - takes the research questions and desired report structure and performs deep research including RAG search, relevancy checks, web research, summarization, and at least one reflection loop where identified gaps are used to create a new research query, search, and summarization
3. `artifact_qa` - takes either the draft queries or the draft report, along with user chat input, and provides for HITL updates to the artifacts or general Q&A about them 

Each step is served as a stand-alone stateless API endpoint using AgentIQ. The frontend manages the user state, tracking the queries and generated artifact over time. 

See the [FAQ](./FAQ.md) for more information on customization or developer options.

### Parallel Search

During the research phase, multiple research questions are searched in parallel. For each query, the RAG service is consulted and an LLM-as-a-judge is used to check the relevancy of the results. If more information is needed, a fallback web search is performed. This search approach ensures internal documents are given preference over generic web results while maintaining accuracy. Performing query search in parallel allows for many data sources to be consulted in an efficient manner.


## Seeding the RAG knowledge base

The frontend is designed to work with custom RAG collections and PDFs as well as two example collections:

- `Financial_Reports` - contains information from public earnings reports for Alphabet, Meta, and Amazon
- `Cystic_Fibrosis_Reports` - contains research publications of Cystic Fibrosis 

To seed these into your RAG database: 

```bash
uv python install 3.12
uv venv --python 3.12 --python-preference managed
uv run pip install -r data/requirements.txt
uv run python data/sync_files2.py
```

