import os
import json
import re
import csv
import box
import yaml
from datetime import datetime

import pandas as pd
import numpy as np
from joblib import dump
import pyarrow as pa

from sklearn.compose import make_column_transformer
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_squared_error, r2_score, make_scorer
from sklearn.model_selection import (
    StratifiedKFold,
    GridSearchCV,
    train_test_split,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import (
    PolynomialFeatures,
    OneHotEncoder,
    FunctionTransformer,
    MinMaxScaler,
)


# Define metric for Grid Search process
# The rmse of the log transformed prices isn't the same as the rmse of the actual prices
# Therefore, I will create a metric that computes the rmse of the actual prices and
# predictions to select the best performing model
def rmse(y_true, y_pred):
    # Compute the rmse of log prices
    rmse_log = np.sqrt(mean_squared_error(y_true, y_pred))
    rmse_value = rmse_log
    #  Sometimes, if the degree is too high for an accurate model, the rmse will yield
    # cray values exceeded 10000 (for reference, for the log prices the average rmse is
    # 0.2)
    # If the value is less than 1000, I will compute the rmse of the actual prices.
    # Otherwise, the output of the function is the rmse of log prices
    if rmse_log < 1000:
        rmse_exp = np.sqrt(mean_squared_error(np.exp(y_true), np.exp(y_pred)))
        rmse_value = rmse_exp
    return rmse_value


def transform_data(sample_data, data):
    # Create data transformer. Note that these
    log_transformer = FunctionTransformer(
        func=np.log, inverse_func=np.exp, validate=True
    )
    transformer = make_column_transformer(
        (log_transformer, cfg.DATA_LOG_COLS),
        (OneHotEncoder(drop="first"), cfg.DATA_OHE_COLS),
        remainder="passthrough",
    )

    # Transform data
    transformer.fit(sample_data)
    transformed_data = transformer.transform(data)

    # Save the transformer to a file
    new_version_transformer = 2
    transformer_name = f"data_transformer-v{new_version_transformer}.joblib"
    dump(transformer, model_data_dir + f"/{transformer_name}")

    # Define column names of new columns created after one-hot-encoding transformation
    ohe_cols_transformed = (
        transformer.named_transformers_["onehotencoder"]
        .get_feature_names_out()
        .tolist()
    )

    return transformer, transformer_name, transformed_data, ohe_cols_transformed


def split_data_and_train_model(transformed_data, ohe_cols_transformed):
    # Define index of column containing fuelType_Hybrid data. It will be used in
    # train_test_split as the stratified variable
    hybrid_idx = (cfg.DATA_LOG_COLS + ohe_cols_transformed).index("fuelType_Hybrid")

    X_data = transformed_data[:, 1:]
    y_data = transformed_data[:, 0]

    X_train, X_test, y_train, y_test = train_test_split(
        X_data,
        y_data,
        test_size=cfg.DATA_SPLIT_TEST_SIZE,
        random_state=cfg.RANDOM_STATE,
        shuffle=True,
        stratify=X_data[:, hybrid_idx - 1],
    )

    # Create stratified split of data using the hybrid index as the stratified variable
    num_splits = 7
    sk_fold = StratifiedKFold(
        n_splits=num_splits, random_state=cfg.RANDOM_STATE, shuffle=True
    )
    folds = sk_fold.split(X_train, X_train[:, hybrid_idx])

    # Define pipeline steps
    steps = [
        ("scaler", MinMaxScaler()),
        ("pol_features", PolynomialFeatures()),
        ("model", LinearRegression()),
    ]

    # Create pipeline object
    pipeline = Pipeline(steps=steps)

    param_grid = {"pol_features__degree": range(1, 6)}

    rmse_scorer = make_scorer(rmse, greater_is_better=False)
    scoring_params = rmse_scorer
    regression_model = GridSearchCV(
        estimator=pipeline,
        param_grid=param_grid,
        scoring=scoring_params,
        cv=folds,
        n_jobs=-1,
        verbose=0,
    )

    regression_model.fit(X_train, y_train)

    # Make predictions on test data and compute metrics
    predictions_log = regression_model.best_estimator_.predict(X_test)
    predictions = np.exp(predictions_log)
    y_test_exp = np.exp(y_test)
    model_test_set_performance = {
        "rmse": f"{np.sqrt(mean_squared_error(y_test_exp, predictions)):.3f}",
        "r2_score": f"{r2_score(y_test, predictions_log):.3f}",
        "r2_score_log_predictions": f"{r2_score(y_test_exp, predictions):.3f}",
    }

    # Make predictions on test data and compute metrics
    predictions_log_all = regression_model.best_estimator_.predict(X_data)
    predictions_all = np.exp(predictions_log_all)
    y_data_exp = np.exp(y_data)
    model_all_data_performance = {
        "rmse": f"{np.sqrt(mean_squared_error(y_data_exp, predictions_all)):.3f}",
        "r2_score": f"{r2_score(y_data, predictions_log_all):.3f}",
        "r2_score_log_predictions": f"{r2_score(y_data_exp, predictions_all):.3f}",
    }

    outlier_index = []
    for i, j in enumerate(predictions_all):
        if (
            predictions_all[i] / y_data_exp[i] > cfg.MODEL_OUTLIER_RATIO_UPPER
            or predictions_all[i] / y_data_exp[i] < cfg.MODEL_OUTLIER_RATIO_LOWER
        ):
            outlier_index.append(i)

    return (
        regression_model,
        model_test_set_performance,
        model_all_data_performance,
        outlier_index,
    )


# Load data
current_dir = os.path.dirname(os.path.abspath(__file__))  # current dir is model folder
model_data_dir = current_dir + "/model_data"
data_files = [
    x for x in os.listdir(model_data_dir) if (".parquet" in x) and ("clean" in x)
]

# Import config vars
config_dir = os.path.join(current_dir, "..", "config/config.yml")
with open(config_dir, "r", encoding="utf8") as ymlfile:
    cfg = box.Box(yaml.safe_load(ymlfile))

# Sort files based on version number and find the latest file
sorted_data_files = sorted(data_files, key=lambda x: int(re.search(r"\d+", x).group()))
latest_data = sorted_data_files[-1]
data = pd.read_parquet(model_data_dir + f"/{latest_data}")


# Load sample data to fit data transformer
new_version_sample_data = 1
sample_data_name = f"sample_data-v{new_version_sample_data}.parquet"
sample_data = pd.read_parquet(model_data_dir + f"/{sample_data_name}")

# Transform the data by applying log transformation to some columns and ohe other
# columns
transformer, transformer_name, transformed_data, ohe_cols_transformed = transform_data(
    sample_data, data
)

# Split data, train model and estimate performance
(
    regression_model,
    model_test_set_performance,
    model_all_data_performance,
    outlier_index,
) = split_data_and_train_model(transformed_data, ohe_cols_transformed)


if outlier_index:
    # Drop the outliers
    data = data.copy()
    data = data.drop(index=outlier_index)
    data.reset_index(drop=True, inplace=True)

    # Save new updated clean data
    data.to_parquet(model_data_dir + f"/{latest_data}")

    # Transform the data by applying log transformation to some columns and ohe other
    # columns
    (
        transformer,
        transformer_name,
        transformed_data,
        ohe_cols_transformed,
    ) = transform_data(sample_data, data)

    # Split data, train model and estimate performance
    (
        regression_model,
        model_test_set_performance,
        model_all_data_performance,
        outlier_index,
    ) = split_data_and_train_model(transformed_data, ohe_cols_transformed)


# Save model
previous_models = [
    x for x in os.listdir(model_data_dir) if (".joblib" in x) and ("car" in x)
]

if previous_models:
    # Sort files based on version number and find the latest file
    sorted_models = sorted(
        previous_models, key=lambda x: int(re.search(r"\d+", x).group())
    )
    previous_model = sorted_models[-1]
    # Extract the last version number and generate the new version number
    previous_version_model = int(
        max([file_name.split("v")[1].split(".")[0] for file_name in previous_models])
    )
    new_version_model = previous_version_model + 1
else:
    # Define variables in case no previous all cars dataset is found
    previous_model = "No previous model"
    new_version_model = 1
model_name = f"car_price-v{new_version_model}"
dump(regression_model.best_estimator_, model_data_dir + f"/{model_name}.joblib")


# Generate data for metadata file
# Filename
# metadata_filename = f"all_scraped_cars-v{new_version}_metadata"
metadata_filename = f"{model_name}_metadata"
# Current date
current_date = datetime.now()
current_date_string = current_date.strftime("%d/%m/%Y")

with open(model_data_dir + "/intermediate_data.json") as f:
    intermediate_data = json.load(f)

metadata = {
    "filename": f"{metadata_filename}.json",
    "creation_date": current_date_string,
    "source_of_data": ["autos", intermediate_data["clean_data_name"]],
    "version": f"v{new_version_model}",
    "associated_model": [
        f"{model_name}.joblib",
        transformer_name,
        intermediate_data["clean_data_name"],
        sample_data_name,
    ],
    "model_polynomial_degree": regression_model.best_estimator_.named_steps[
        "pol_features"
    ].degree,
    "model_metrics": {
        "test_set": model_test_set_performance,
        "train_test_sets": model_all_data_performance,
    },
    "model_scoring": cfg.MODEL_SCORING_FUNCTION,
    "outlier_limits": {
        "lowest_year": intermediate_data["lowest_year"],
        "car_max_prices": {
            "audi": intermediate_data["audi_max_price"],
            "bmw": intermediate_data["bmw_max_price"],
            "volkswagen": intermediate_data["vw_max_price"],
        },
        "mileage_limits": {
            "min_mileage": intermediate_data["min_mileage"],
            "max_mileage": intermediate_data["max_mileage"],
        },
        "mpg_limits": {
            "low": intermediate_data["low_mpg"],
            "high": intermediate_data["high_mpg"],
        },
        "engine_size_limits": {
            "low": intermediate_data["low_engine_size"],
            "high": intermediate_data["high_engine_size"],
        },
        "fuel_type_removed": intermediate_data["fuel_type_remove"],
    },
}

# Save the metadata to a JSON file
metadata_path = model_data_dir + f"/{metadata_filename}.json"
with open(metadata_path, "w") as file:
    json.dump(metadata, file, indent=4)

# Remove file with intermediate data for metadata file
os.remove(model_data_dir + "/intermediate_data.json")
