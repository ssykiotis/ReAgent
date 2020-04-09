#!/usr/bin/env python3

import logging
from typing import Dict, List, Optional, Tuple

from ml.rl import types as rlt
from ml.rl.core.dataclasses import dataclass, field
from ml.rl.evaluation.evaluator import Evaluator, get_metrics_to_score
from ml.rl.models.base import ModelBase
from ml.rl.parameters import NormalizationData
from ml.rl.preprocessing.batch_preprocessor import BatchPreprocessor
from ml.rl.workflow.data_fetcher import query_data
from ml.rl.workflow.identify_types_flow import identify_normalization_parameters
from ml.rl.workflow.model_managers.model_manager import ModelManager
from ml.rl.workflow.reporters.discrete_dqn_reporter import DiscreteDQNReporter
from ml.rl.workflow.types import (
    Dataset,
    PreprocessingOptions,
    ReaderOptions,
    RewardOptions,
    RLTrainingOutput,
    TableSpec,
)
from ml.rl.workflow.utils import train_and_evaluate_generic
from ml.rl.workflow_utils.page_handler import EvaluationPageHandler, TrainingPageHandler


logger = logging.getLogger(__name__)


class DiscreteNormalizationParameterKeys:
    STATE = "state"


@dataclass
class DiscreteDQNBase(ModelManager):
    target_action_distribution: Optional[List[float]] = None
    state_feature_config: Optional[rlt.ModelFeatureConfig] = field(
        default_factory=lambda: rlt.ModelFeatureConfig(float_feature_infos=[])
    )
    preprocessing_options: Optional[PreprocessingOptions] = None
    reader_options: Optional[ReaderOptions] = None

    def __post_init_post_parse__(self):
        super().__init__()
        self._metrics_to_score = None
        self._q_network: Optional[ModelBase] = None

    @classmethod
    def normalization_key(cls) -> str:
        return DiscreteNormalizationParameterKeys.STATE

    @property
    def metrics_to_score(self) -> List[str]:
        assert self.reward_options is not None
        if self._metrics_to_score is None:
            self._metrics_to_score = get_metrics_to_score(
                self._reward_options.metric_reward_values
            )
        return self._metrics_to_score

    @property
    def should_generate_eval_dataset(self) -> bool:
        return self.eval_parameters.calc_cpe_in_training

    def _set_normalization_parameters(
        self, normalization_data_map: Dict[str, NormalizationData]
    ):
        """
        Set normalization parameters on current instance
        """
        state_norm_data = normalization_data_map.get(self.normalization_key(), None)
        assert state_norm_data is not None
        assert state_norm_data.dense_normalization_parameters is not None
        self.state_normalization_parameters = (
            state_norm_data.dense_normalization_parameters
        )

    def run_feature_identification(
        self, input_table_spec: TableSpec
    ) -> Dict[str, NormalizationData]:
        preprocessing_options = self.preprocessing_options or PreprocessingOptions()
        logger.info("Overriding whitelist_features")
        state_features = [
            ffi.feature_id for ffi in self.state_feature_config.float_feature_infos
        ]
        preprocessing_options = preprocessing_options._replace(
            whitelist_features=state_features
        )

        state_normalization_parameters = identify_normalization_parameters(
            input_table_spec, "state_features", preprocessing_options
        )
        return {
            DiscreteNormalizationParameterKeys.STATE: NormalizationData(
                dense_normalization_parameters=state_normalization_parameters
            )
        }

    def query_data(
        self,
        input_table_spec: TableSpec,
        sample_range: Optional[Tuple[float, float]],
        reward_options: RewardOptions,
        eval_dataset: bool,
    ) -> Dataset:
        # sort is set to False because EvaluationPageHandler sort the data anyway
        return query_data(
            input_table_spec,
            self.action_names,
            self.rl_parameters.use_seq_num_diff_as_time_diff,
            sample_range=sample_range,
            metric_reward_values=reward_options.metric_reward_values,
            custom_reward_expression=reward_options.custom_reward_expression,
            additional_reward_expression=reward_options.additional_reward_expression,
            multi_steps=self.multi_steps,
            gamma=self.rl_parameters.gamma,
            sort=False,
        )

    @property
    def multi_steps(self) -> Optional[int]:
        return self.rl_parameters.multi_steps

    def build_batch_preprocessor(self) -> BatchPreprocessor:
        raise NotImplementedError

    def train(
        self, train_dataset: Dataset, eval_dataset: Optional[Dataset], num_epochs: int
    ) -> RLTrainingOutput:
        """
        Train the model

        Returns partially filled RLTrainningOutput. The field that should not be filled
        are:
        - output_path
        - warmstart_output_path
        - vis_metrics
        - validation_output
        """
        logger.info("Creating reporter")
        reporter = DiscreteDQNReporter(
            self.trainer_param.actions,
            target_action_distribution=self.target_action_distribution,
        )
        logger.info("Adding reporter to trainer")
        self.trainer.add_observer(reporter)

        training_page_handler = TrainingPageHandler(self.trainer)
        training_page_handler.add_observer(reporter)
        evaluator = Evaluator(
            self.action_names,
            self.rl_parameters.gamma,
            self.trainer,
            metrics_to_score=self.metrics_to_score,
        )
        logger.info("Adding reporter to evaluator")
        evaluator.add_observer(reporter)
        evaluation_page_handler = EvaluationPageHandler(
            self.trainer, evaluator, reporter
        )

        batch_preprocessor = self.build_batch_preprocessor()
        train_and_evaluate_generic(
            train_dataset,
            eval_dataset,
            self.trainer,
            num_epochs,
            self.use_gpu,
            batch_preprocessor,
            training_page_handler,
            evaluation_page_handler,
            reader_options=self.reader_options,
        )
        return RLTrainingOutput(training_report=reporter.generate_training_report())
