#!/bin/bash

# Ensure .env exists
if [ ! -f ".env" ]; then
    echo "Error: .env file not found. Please copy .env.example to .env and configure it."
    exit 1
fi

# Load variables from .env
export $(grep -v '^#' .env | xargs)

# Fallback if GCP_PROJECT_ID is not in .env or is default
if [ -z "$GCP_PROJECT_ID" ] || [ "$GCP_PROJECT_ID" == "your-project-id" ]; then
    GCP_PROJECT_ID=$(gcloud config get-value project)
fi

echo "Using Project ID: $GCP_PROJECT_ID"

# Check if GEMINI_API_KEY secret exists in Secret Manager
echo "Checking Secret Manager for GEMINI_API_KEY..."
if ! gcloud secrets describe GEMINI_API_KEY --project="$GCP_PROJECT_ID" &> /dev/null; then
    echo "Error: Secret 'GEMINI_API_KEY' not found in Secret Manager for project $GCP_PROJECT_ID."
    echo "Please create it first: gcloud secrets create GEMINI_API_KEY --replication-policy=\"automatic\""
    echo "Then add a version: echo -n \"your-api-key\" | gcloud secrets versions add GEMINI_API_KEY --data-file=-"
    exit 1
fi

# 1. Create GCS Bucket if it doesn't exist
echo "Checking/Creating GCS Bucket: $GCS_BUCKET_NAME"
if ! gcloud storage ls "gs://$GCS_BUCKET_NAME" --project="$GCP_PROJECT_ID" &> /dev/null; then
    echo "Creating bucket gs://$GCS_BUCKET_NAME..."
    gcloud storage buckets create "gs://$GCS_BUCKET_NAME" --project="$GCP_PROJECT_ID" --location="$GCP_REGION"
    
    # Make bucket readable/writable by the default compute service account
    PROJECT_NUMBER=$(gcloud projects describe $GCP_PROJECT_ID --format="value(projectNumber)")
    SA_EMAIL="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
    gcloud storage buckets add-iam-policy-binding gs://$GCS_BUCKET_NAME \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="roles/storage.admin"
else
    echo "Bucket gs://$GCS_BUCKET_NAME already exists."
fi

# 2. Deploy to Cloud Run
echo "Deploying to Cloud Run..."
gcloud run deploy $SERVICE_NAME \
    --source . \
    --project "$GCP_PROJECT_ID" \
    --region "$GCP_REGION" \
    --allow-unauthenticated \
    --set-env-vars="GCS_BUCKET_NAME=$GCS_BUCKET_NAME" \
    --set-secrets="GEMINI_API_KEY=GEMINI_API_KEY:latest"

echo "Deployment complete."
