# ai_models/model_manager.py
import mlflow
import mlflow.sklearn
import logging
import os  # For paths
import pickle  # For model persistence.
from config.settings import MLFLOW_TRACKING_URI, MLFLOW_EXPERIMENT_NAME  # Set these in settings.py
# Model loading.
from ai_models.lstm_model import create_lstm_model
from ai_models.garch_model import train_garch_model, predict_volatility_garch
from ai_models.bert_sentiment import load_sentiment_analysis_model, analyze_sentiment

logger = logging.getLogger(__name__)

# --- MLflow Configuration ---
mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
mlflow.set_experiment(MLFLOW_EXPERIMENT_NAME)  # Create your experiment in MLFlow

def log_model_mlflow(model, model_name, metrics=None, artifacts=None, scaler=None):
    """Logs the models."""
    try:
        with mlflow.start_run(run_name=f"Training {model_name}") as run:
            if metrics:
                mlflow.log_metrics(metrics) #Log the metrics

            # Log the scaler if it exists.
            if scaler is not None:
                with open("scaler.pkl", "wb") as f:
                    pickle.dump(scaler, f)  # save
                mlflow.log_artifact("scaler.pkl", artifact_path="scalers")

            # Use pickle for the others
            with open(f"{model_name}.pkl", "wb") as f:
                pickle.dump(model, f)
            mlflow.log_artifact(f"{model_name}.pkl", artifact_path="models")

            if artifacts:
                for artifact_name, artifact_path in artifacts.items():
                    mlflow.log_artifact(artifact_path,
                                        artifact_path=artifact_name)
            run_id = run.info.run_uuid
            logger.info(
                f"MLflow: Model '{model_name}' logged successfully. Run ID: {run_id}")
    except Exception as e:
        logger.error(f"MLflow logging error: {e}")

def load_model_mlflow(model_name, model_type="lstm"):
    """Loads the model."""
    try:
        # Get the latest version
        client = mlflow.tracking.MlflowClient()
        experiment = client.get_experiment_by_name(
            MLFLOW_EXPERIMENT_NAME)
        if not experiment:
            logger.error(
                f"MLflow: Experiment '{MLFLOW_EXPERIMENT_NAME}' not found."
            )
            return None, None, None
        runs = client.search_runs(
            experiment_ids=[experiment.experiment_id],
            filter_string=f"tags.mlflow.runName = 'Training {model_name}'",  #Find by name
            order_by=["end_time DESC"],
            max_results=1,
        )
        if not runs or not runs[0].info.run_uuid:
            logger.warning(f"MLflow: No runs found for model '{model_name}'.")
            return None, None, None
        run_id = runs[0].info.run_uuid
        model_uri = f"runs:/{run_id}/models/{model_name}.pkl"
        # Load model
        model_path = f"{model_name}.pkl"
        client.download_artifacts(run_id, "models", model_path)  # Download.
        with open(model_path, "rb") as f:
            model = pickle.load(f)
        #Load the scaler if it exists.
        scaler_path = "scaler.pkl"
        try: #Try.
            client.download_artifacts(run_id, "scalers", scaler_path)  # Download the scaler
            with open(scaler_path, "rb") as f:
                scaler = pickle.load(f)
        except Exception as e:
            scaler = None
            logger.info(f"MLFlow: No scaler found for the model: {model_name}")

        return model, scaler, run_id
    except Exception as e:
        logger.error(f"MLflow model loading error: {e}")
        return None, None, None