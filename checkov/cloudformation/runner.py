import logging
import os

from checkov.cloudformation import cfn_utils
from checkov.cloudformation.cfn_utils import create_file_abs_path, create_definitions, build_definitions_context
from checkov.cloudformation.checks.resource.registry import cfn_registry
from checkov.cloudformation.context_parser import ContextParser
from checkov.cloudformation.graph_builder.graph_components.block_types import CloudformationTemplateSections
from checkov.cloudformation.graph_builder.graph_to_definitions import convert_graph_vertices_to_definitions
from checkov.cloudformation.graph_builder.local_graph import CloudformationLocalGraph
from checkov.cloudformation.graph_manager import CloudformationGraphManager
from checkov.common.checks_infra.registry import get_graph_checks_registry
from checkov.common.graph.db_connectors.networkx.networkx_db_connector import NetworkxConnector
from checkov.common.graph.graph_builder import CustomAttributes
from checkov.common.output.graph_record import GraphRecord
from checkov.common.output.record import Record
from checkov.common.output.report import Report, merge_reports
from checkov.common.runners.base_runner import BaseRunner
from checkov.runner_filter import RunnerFilter


class Runner(BaseRunner):
    check_type = "cloudformation"

    def __init__(self, db_connector=NetworkxConnector(),
                 source="CloudFormation",
                 graph_class=CloudformationLocalGraph,
                 graph_manager=None,
                 external_registries=None):
        self.external_registries = [] if external_registries is None else external_registries
        self.graph_class = graph_class
        self.graph_manager = graph_manager if graph_manager is not None else CloudformationGraphManager(source=source,
                                                                                                        db_connector=db_connector)
        self.definitions_raw = {}
        self.graph_registry = get_graph_checks_registry(self.check_type)

    def run(self, root_folder, external_checks_dir=None, files=None, runner_filter=RunnerFilter(), collect_skip_comments=True):
        report = Report(self.check_type)

        if self.context is None or self.definitions is None or self.breadcrumbs is None:

            self.definitions, self.definitions_raw = create_definitions(root_folder, files, runner_filter)
            if external_checks_dir:
                for directory in external_checks_dir:
                    cfn_registry.load_external_checks(directory)

        self.context = build_definitions_context(self.definitions, self.definitions_raw, root_folder)

        # run graph checks only if environment variable CHECKOV_CLOUDFORMATION_GRAPH='true'
        if os.getenv("CHECKOV_CLOUDFORMATION_GRAPH", "false").lower() == "true":
            logging.info("creating cloudformation graph")
            local_graph = self.graph_manager.build_graph_from_definitions(self.definitions)
            self.graph_manager.save_graph(local_graph)
            self.definitions, self.breadcrumbs = convert_graph_vertices_to_definitions(local_graph.vertices, root_folder)

        for cf_file, definition in self.definitions.items():

            file_abs_path = create_file_abs_path(root_folder, cf_file)

            if isinstance(definition, dict) and CloudformationTemplateSections.RESOURCES in definition.keys():
                cf_context_parser = ContextParser(cf_file, definition, self.definitions_raw[cf_file])
                for resource_name, resource in definition[CloudformationTemplateSections.RESOURCES].items():
                    resource_id = cf_context_parser.extract_cf_resource_id(resource, resource_name)
                    # check that the resource can be parsed as a CF resource
                    if resource_id:
                        entity_lines_range, entity_code_lines = cf_context_parser.extract_cf_resource_code_lines(resource)
                        if entity_lines_range and entity_code_lines:
                            # TODO - Variable Eval Message!
                            variable_evaluations = {}

                            skipped_checks = ContextParser.collect_skip_comments(entity_code_lines)
                            entity = {resource_name: resource}
                            results = cfn_registry.scan(cf_file, entity, skipped_checks,
                                                        runner_filter)
                            tags = cfn_utils.get_resource_tags(entity)
                            for check, check_result in results.items():
                                record = Record(check_id=check.id, bc_check_id=check.bc_id, check_name=check.name, check_result=check_result,
                                                code_block=entity_code_lines, file_path=cf_file,
                                                file_line_range=entity_lines_range, resource=resource_id,
                                                evaluations=variable_evaluations,check_class=check.__class__.__module__,
                                                file_abs_path=file_abs_path, entity_tags=tags)
                                report.add_record(record=record)

        # run graph checks only if environment variable CHECKOV_CLOUDFORMATION_GRAPH='true'
        if os.getenv("CHECKOV_CLOUDFORMATION_GRAPH", "false").lower() == "true":
            graph_report = self.get_graph_checks_report(root_folder, runner_filter)
            merge_reports(report, graph_report)

        return report

    def get_graph_checks_report(self, root_folder, runner_filter: RunnerFilter):
        report = Report(self.check_type)
        checks_results = self.run_graph_checks_results(runner_filter)

        for check, check_results in checks_results.items():
            for check_result in check_results:
                entity = check_result['entity']
                entity_file_abs_path = create_file_abs_path(root_folder, entity.get(CustomAttributes.FILE_PATH))
                entity_name = entity.get(CustomAttributes.BLOCK_NAME).split(".")[1]
                entity_context = self.context[entity_file_abs_path][CloudformationTemplateSections.RESOURCES][entity_name]

                record = Record(check_id=check.id,
                                check_name=check.name,
                                check_result=check_result,
                                code_block=entity_context.get("code_lines"),
                                file_path=entity.get(CustomAttributes.FILE_PATH),
                                file_line_range=[entity_context.get("start_line"), entity_context.get("end_line")],
                                resource=entity.get(CustomAttributes.ID),
                                evaluations={},
                                check_class=check.__class__.__module__,
                                file_abs_path=entity_file_abs_path,
                                entity_tags={} if not entity.get("Tags") else cfn_utils.parse_entity_tags(entity.get("Tags")))
                if self.breadcrumbs:
                    breadcrumb = self.breadcrumbs.get(record.file_path, {}).get(record.resource)
                    if breadcrumb:
                        record = GraphRecord(record, breadcrumb)

                report.add_record(record=record)
        return report
