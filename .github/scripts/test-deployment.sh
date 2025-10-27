#!/bin/bash
###############################################################################
# Biomedical AI-Q Research Agent - Deployment Test Script
# Purpose: Verify deployment and run functional tests
###############################################################################

set -e

# Color definitions
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Test counters
TESTS_PASSED=0
TESTS_FAILED=0

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[PASS]${NC} $1"
    ((TESTS_PASSED++))
}

log_error() {
    echo -e "${RED}[FAIL]${NC} $1"
    ((TESTS_FAILED++))
}

log_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

# Wait for service to be ready
wait_for_service() {
    local url=$1
    local service_name=$2
    local max_attempts=30
    local attempt=0
    
    log_info "Waiting for $service_name to be ready..."
    
    while [ $attempt -lt $max_attempts ]; do
        if curl -sf "$url" > /dev/null 2>&1; then
            log_success "$service_name is ready"
            return 0
        fi
        ((attempt++))
        echo -n "."
        sleep 2
    done
    
    log_error "$service_name failed to become ready within timeout"
    return 1
}

# Test container status
test_containers() {
    log_info "=========================================="
    log_info "Test 1: Container Status Check"
    log_info "=========================================="
    
    local required_containers=(
        "aira-frontend"
        "aira-nginx"
        "aira-backend"
        "rag-server"
        "rag-playground"
        "ingestor-server"
        "milvus-standalone"
    )
    
    for container in "${required_containers[@]}"; do
        if docker ps --format '{{.Names}}' | grep -q "^${container}$"; then
            log_success "Container $container is running"
        else
            log_error "Container $container is not running"
        fi
    done
}

# Test service endpoints
test_endpoints() {
    log_info "=========================================="
    log_info "Test 2: Service Endpoint Accessibility"
    log_info "=========================================="
    
    # Test frontend
    if curl -sf http://localhost:3001 > /dev/null 2>&1; then
        log_success "Frontend (3001) is accessible"
    else
        log_error "Frontend (3001) is not accessible"
    fi
    
    # Test backend API
    if curl -sf http://localhost:8051/docs > /dev/null 2>&1; then
        log_success "Backend API (8051) is accessible"
    else
        log_error "Backend API (8051) is not accessible"
    fi
    
    # Test RAG Playground
    if curl -sf http://localhost:8090 > /dev/null 2>&1; then
        log_success "RAG Playground (8090) is accessible"
    else
        log_error "RAG Playground (8090) is not accessible"
    fi
    
    # Test RAG Server
    if curl -sf http://localhost:8081/health > /dev/null 2>&1; then
        log_success "RAG Server (8081) health check passed"
    else
        log_error "RAG Server (8081) health check failed"
    fi
}

# Test collection creation
test_create_collection() {
    log_info "=========================================="
    log_info "Test 3: Create Collection"
    log_info "=========================================="
    
    local response=$(curl -s -w "\n%{http_code}" -X POST http://localhost:8051/v1/collections \
        -H 'accept: application/json' \
        -H 'Content-Type: application/json' \
        -d '["test_collection_ci"]')
    
    local http_code=$(echo "$response" | tail -n1)
    local body=$(echo "$response" | head -n-1)
    
    if [ "$http_code" == "200" ] || [ "$http_code" == "201" ]; then
        log_success "Collection created successfully (HTTP $http_code)"
        echo "Response: $body"
    else
        log_error "Collection creation failed (HTTP $http_code)"
        echo "Response: $body"
    fi
    
    sleep 3
}

# Test document upload
test_upload_document() {
    log_info "=========================================="
    log_info "Test 4: Upload Document"
    log_info "=========================================="
    
    if [ -f "notebooks/simple.pdf" ]; then
        local response=$(curl -s -w "\n%{http_code}" -X POST http://localhost:8051/v1/documents \
            -H 'accept: application/json' \
            -F 'documents=@notebooks/simple.pdf' \
            -F 'data={"collection_name": "test_collection_ci"}')
        
        local http_code=$(echo "$response" | tail -n1)
        local body=$(echo "$response" | head -n-1)
        
        if [ "$http_code" == "200" ] || [ "$http_code" == "201" ]; then
            log_success "Document uploaded successfully (HTTP $http_code)"
            echo "Response: $body"
        else
            log_error "Document upload failed (HTTP $http_code)"
            echo "Response: $body"
        fi
    else
        log_error "Test file notebooks/simple.pdf not found"
    fi
    
    sleep 5
}

# Test RAG query
test_rag_query() {
    log_info "=========================================="
    log_info "Test 5: RAG Query"
    log_info "=========================================="
    
    local response=$(curl -s -w "\n%{http_code}" -X POST "http://localhost:8081/v1/generate" \
        -H 'accept: application/json' \
        -H 'Content-Type: application/json' \
        -d '{
            "messages": [
                {
                    "role": "user",
                    "content": "What is the title?"
                }
            ],
            "use_knowledge_base": true,
            "temperature": 0.2,
            "top_p": 0.7,
            "max_tokens": 512,
            "collection_name": "test_collection_ci",
            "enable_citations": true
        }')
    
    local http_code=$(echo "$response" | tail -n1)
    
    if [ "$http_code" == "200" ]; then
        log_success "RAG query succeeded (HTTP $http_code)"
    else
        log_error "RAG query failed (HTTP $http_code)"
    fi
}

# Test research plan generation
test_generate_query() {
    log_info "=========================================="
    log_info "Test 6: Generate Research Plan"
    log_info "=========================================="
    
    local response=$(curl -s -w "\n%{http_code}" -X POST http://localhost:8051/generate_query/stream \
        -H 'accept: application/json' \
        -H 'Content-Type: application/json' \
        -d '{
            "topic": "AI in Healthcare",
            "report_organization": "A brief report with introduction and conclusion",
            "num_queries": 2,
            "llm_name": "nemotron"
        }')
    
    local http_code=$(echo "$response" | tail -n1)
    
    if [ "$http_code" == "200" ]; then
        log_success "Research plan generation succeeded (HTTP $http_code)"
    else
        log_error "Research plan generation failed (HTTP $http_code)"
    fi
}

# Test Q&A functionality
test_artifact_qa() {
    log_info "=========================================="
    log_info "Test 7: Artifact Q&A"
    log_info "=========================================="
    
    local response=$(curl -s -w "\n%{http_code}" -X POST http://localhost:8051/artifact_qa/stream \
        -H "accept: application/json" \
        -H "Content-Type: application/json" \
        -d '{
            "additional_context": "",
            "artifact": "# Test Report\n\nThis is a test report.",
            "chat_history": [],
            "question": "What is this report about?",
            "rewrite_mode": "entire",
            "use_internet": false,
            "rag_collection": "test_collection_ci"
        }')
    
    local http_code=$(echo "$response" | tail -n1)
    
    if [ "$http_code" == "200" ]; then
        log_success "Q&A functionality succeeded (HTTP $http_code)"
    else
        log_error "Q&A functionality failed (HTTP $http_code)"
    fi
}

# Main test flow
main() {
    echo ""
    log_info "Starting Biomedical AI-Q Research Agent Deployment Tests"
    echo ""
    
    # Wait for services to be ready
    wait_for_service "http://localhost:3001" "Frontend"
    wait_for_service "http://localhost:8051/docs" "Backend API"
    wait_for_service "http://localhost:8090" "RAG Playground"
    
    echo ""
    
    # Run tests
    test_containers
    echo ""
    
    test_endpoints
    echo ""
    
    test_create_collection
    echo ""
    
    test_upload_document
    echo ""
    
    test_rag_query
    echo ""
    
    test_generate_query
    echo ""
    
    test_artifact_qa
    echo ""
    
    # Test summary
    log_info "=========================================="
    log_info "Test Summary"
    log_info "=========================================="
    echo -e "${GREEN}Passed: $TESTS_PASSED${NC}"
    echo -e "${RED}Failed: $TESTS_FAILED${NC}"
    echo ""
    
    if [ $TESTS_FAILED -eq 0 ]; then
        log_info "All tests passed! Deployment successful."
        exit 0
    else
        log_error "$TESTS_FAILED test(s) failed"
        exit 1
    fi
}

# Run main function
main
