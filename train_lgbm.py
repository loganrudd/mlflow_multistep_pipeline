import numpy as np
from sklearn.model_selection import RandomizedSearchCV
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.metrics import roc_auc_score
import argparse
import pyspark
import mlflow.sklearn
import lightgbm as lgb
import yaml


def fit_sklearn_crossvalidator(loans_parquet_uri, config, split_prop):
    """
    Helper function that fits a scikit-learn 5-fold cross validated model
    to predict a binary label `target` on the passed-in training DataFrame
    using the columns in `features`
    :param: train: Spark DataFrame containing training data
    :param: features: List of strings containing column names to use as
            features from `train`
    :param: target: String name of binary target column of `train` to predict
    :param: model: A scikit-learn estimator
    :param: param_grid: A python dict of model parameters to search through in
            RandomizedSearchCV
    """

    with open(config) as f:
        loaded_config = yaml.full_load(f)

    features = loaded_config['features']
    target = loaded_config['target']
    seed = 7
    spark = pyspark.sql.SparkSession.builder.getOrCreate()

    loans_df = spark.read.parquet(loans_parquet_uri)
    train_df, test_df = loans_df.randomSplit(
        [split_prop, 1 - split_prop], seed=seed)
    train_df.cache()
    test_df.cache()

    with mlflow.start_run() as run:
        mlflow.log_metric("training_nrows", train_df.count())
        mlflow.log_metric("test_nrows", test_df.count())

        print('Training: {0}, test: {1}'.format(train_df.count(),
                                                test_df.count()))

        # include target in training data and convert spark DF to pandas DF
        train_df = train_df.select(features + target).toPandas()
        # drop bad row
        #train_df.drop(index=36905, inplace=True)

        test_df = test_df.select(features + target).toPandas()

        lgbm = lgb.LGBMClassifier(n_jobs=2)

        param_space = loaded_config['parameter_space']
        param_grid = {'__max_depth': param_space['max_depth'],
                      '__min_split_gain': param_space['gamma'],
                      '__min_child_weight': param_space['min_child_weight'],
                      '__learning_rate': param_space['learning_rate'],
                      '__colsample_bytree': param_space['colsample_bytree'],
                      '__num_leaves': param_space['num_leaves']
                      }
        # define pipeline and initialize RandomizedSearchCV
        steps = [('imputation', SimpleImputer()),
                 ('', lgbm)]
        pipeline = Pipeline(steps)
        crossval = RandomizedSearchCV(pipeline,
                                      param_grid,
                                      scoring='roc_auc',
                                      n_jobs=4,
                                      n_iter=100,
                                      random_state=seed,
                                      cv=5)



        mlflow.log_param("features", features)
        mlflow.log_param("split_prop", split_prop)

        # fit and log best estimator
        cvModel = crossval.fit(train_df.drop(target, axis=1),
                               train_df[target].values.ravel())
        mlflow.sklearn.log_model(cvModel.best_estimator_,
                                 "best-5-fold-cross-validated-{}" \
                                 .format(str(lgbm).split('(')[0]))

        y_pred = cvModel.best_estimator_.predict(test_df.drop(target, axis=1))
        print('Train ROC: {:.3f}'.format(cvModel.best_score_))
        test_roc = roc_auc_score(test_df[target], y_pred)
        print('Test ROC: {:.3f}'.format(test_roc))
        # log metric(s), if multiple can be as a dict
        mlflow.log_metrics({"ROC_AUC_train": cvModel.best_score_,
                           "ROC_AUC_test": test_roc})

        # log parameters of best fit model
        for key in cvModel.best_params_:
            mlflow.log_param(key, cvModel.best_params_[key])

        # print run info
        runID = run.info.run_uuid
        experimentID = run.info.experiment_id

        print("Inside MLflow Run with run_id {} and experiment_id {}"
              .format(runID, experimentID))

        return cvModel

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("-u", "--loans_parquet_uri")
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("-s", "--split_prop", default=0.8, type=float)
    args = parser.parse_args()

    fit_sklearn_crossvalidator(args.loans_parquet_uri,
                               args.config,
                               args.split_prop)