import copy
import json
import time
from hashlib import sha256
from typing import Optional
from typing import Tuple
from typing import TypedDict
from typing import Union
from uuid import UUID

from flask import current_app
from SpiffWorkflow.bpmn.serializer.workflow import BpmnWorkflow  # type: ignore
from SpiffWorkflow.bpmn.serializer.workflow import BpmnWorkflowSerializer
from SpiffWorkflow.exceptions import WorkflowException  # type: ignore
from SpiffWorkflow.task import Task as SpiffTask  # type: ignore
from SpiffWorkflow.task import TaskState
from SpiffWorkflow.task import TaskStateNames
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from spiffworkflow_backend.models.bpmn_process import BpmnProcessModel
from spiffworkflow_backend.models.bpmn_process import BpmnProcessNotFoundError
from spiffworkflow_backend.models.bpmn_process_definition import BpmnProcessDefinitionModel
from spiffworkflow_backend.models.db import db
from spiffworkflow_backend.models.json_data import JsonDataModel  # noqa: F401
from spiffworkflow_backend.models.process_instance import ProcessInstanceModel
from spiffworkflow_backend.models.process_instance_event import ProcessInstanceEventModel
from spiffworkflow_backend.models.process_instance_event import ProcessInstanceEventType
from spiffworkflow_backend.models.spec_reference import SpecReferenceCache
from spiffworkflow_backend.models.spec_reference import SpecReferenceNotFoundError
from spiffworkflow_backend.models.task import TaskModel  # noqa: F401
from spiffworkflow_backend.models.task_definition import TaskDefinitionModel
from spiffworkflow_backend.services.process_instance_tmp_service import ProcessInstanceTmpService


class StartAndEndTimes(TypedDict):
    start_in_seconds: Optional[float]
    end_in_seconds: Optional[float]


class JsonDataDict(TypedDict):
    hash: str
    data: dict


class TaskModelError(Exception):
    """Copied from SpiffWorkflow.exceptions.WorkflowTaskException.

    Reimplements the exception from SpiffWorkflow to not require a spiff_task.
    """

    def __init__(
        self,
        error_msg: str,
        task_model: TaskModel,
        exception: Optional[Exception] = None,
        line_number: Optional[int] = None,
        offset: Optional[int] = None,
        error_line: Optional[str] = None,
    ):
        self.task_model = task_model
        self.line_number = line_number
        self.offset = offset
        self.error_line = error_line
        self.notes: list[str] = []

        if exception:
            self.error_type = exception.__class__.__name__
        else:
            self.error_type = "unknown"

        if isinstance(exception, SyntaxError) and not line_number:
            self.line_number = exception.lineno
            self.offset = exception.offset
        elif isinstance(exception, NameError):
            self.add_note(
                WorkflowException.did_you_mean_from_name_error(exception, list(task_model.get_data().keys()))
            )

        # If encountered in a sub-workflow, this traces back up the stack,
        # so we can tell how we got to this particular task, no matter how
        # deeply nested in sub-workflows it is.  Takes the form of:
        # task-description (file-name)
        self.task_trace = self.get_task_trace(task_model)

    def add_note(self, note: str) -> None:
        self.notes.append(note)

    def __str__(self) -> str:
        """Add notes to the error message."""
        return super().__str__() + ". " + ". ".join(self.notes)

    @classmethod
    def get_task_trace(cls, task_model: TaskModel) -> list[str]:
        task_definition = task_model.task_definition
        task_bpmn_name = TaskService.get_name_for_display(task_definition)
        bpmn_process = task_model.bpmn_process
        spec_reference = TaskService.get_spec_reference_from_bpmn_process(bpmn_process)

        task_trace = [f"{task_bpmn_name} ({spec_reference.file_name})"]
        while bpmn_process.guid is not None:
            caller_task_model = TaskModel.query.filter_by(guid=bpmn_process.guid).first()
            bpmn_process = BpmnProcessModel.query.filter_by(id=bpmn_process.direct_parent_process_id).first()
            spec_reference = TaskService.get_spec_reference_from_bpmn_process(bpmn_process)
            task_trace.append(
                f"{TaskService.get_name_for_display(caller_task_model.task_definition)} ({spec_reference.file_name})"
            )
        return task_trace


class TaskService:
    PYTHON_ENVIRONMENT_STATE_KEY = "spiff__python_env_state"

    def __init__(
        self,
        process_instance: ProcessInstanceModel,
        serializer: BpmnWorkflowSerializer,
        bpmn_definition_to_task_definitions_mappings: dict,
    ) -> None:
        self.process_instance = process_instance
        self.bpmn_definition_to_task_definitions_mappings = bpmn_definition_to_task_definitions_mappings
        self.serializer = serializer

        self.bpmn_processes: dict[str, BpmnProcessModel] = {}
        self.task_models: dict[str, TaskModel] = {}
        self.json_data_dicts: dict[str, JsonDataDict] = {}
        self.process_instance_events: dict[str, ProcessInstanceEventModel] = {}

    def save_objects_to_database(self) -> None:
        db.session.bulk_save_objects(self.bpmn_processes.values())
        db.session.bulk_save_objects(self.task_models.values())
        db.session.bulk_save_objects(self.process_instance_events.values())
        self.__class__.insert_or_update_json_data_records(self.json_data_dicts)

    def process_parents_and_children_and_save_to_database(
        self,
        spiff_task: SpiffTask,
    ) -> None:
        self.process_spiff_task_children(spiff_task)
        self.process_spiff_task_parent_subprocess_tasks(spiff_task)
        self.save_objects_to_database()

    def process_spiff_task_children(
        self,
        spiff_task: SpiffTask,
    ) -> None:
        for child_spiff_task in spiff_task.children:
            if child_spiff_task._has_state(TaskState.PREDICTED_MASK):
                self.__class__.remove_spiff_task_from_parent(child_spiff_task, self.task_models)
                continue
            self.update_task_model_with_spiff_task(
                spiff_task=child_spiff_task,
            )
            self.process_spiff_task_children(
                spiff_task=child_spiff_task,
            )

    def process_spiff_task_parent_subprocess_tasks(
        self,
        spiff_task: SpiffTask,
    ) -> None:
        """Find the parent subprocess of a given spiff_task and update its data.

        This will also process that subprocess task's children and will recurse upwards
        to process its parent subprocesses as well.
        """
        (parent_subprocess_guid, _parent_subprocess) = self.__class__._task_subprocess(spiff_task)
        if parent_subprocess_guid is not None:
            spiff_task_of_parent_subprocess = spiff_task.workflow._get_outermost_workflow().get_task_from_id(
                UUID(parent_subprocess_guid)
            )

            if spiff_task_of_parent_subprocess is not None:
                self.update_task_model_with_spiff_task(
                    spiff_task=spiff_task_of_parent_subprocess,
                )
                self.process_spiff_task_children(
                    spiff_task=spiff_task_of_parent_subprocess,
                )
                self.process_spiff_task_parent_subprocess_tasks(
                    spiff_task=spiff_task_of_parent_subprocess,
                )

    def update_task_model_with_spiff_task(
        self,
        spiff_task: SpiffTask,
        start_and_end_times: Optional[StartAndEndTimes] = None,
    ) -> TaskModel:
        new_bpmn_process = None
        if str(spiff_task.id) in self.task_models:
            task_model = self.task_models[str(spiff_task.id)]
        else:
            (
                new_bpmn_process,
                task_model,
            ) = self.find_or_create_task_model_from_spiff_task(
                spiff_task,
            )

        # we are not sure why task_model.bpmn_process can be None while task_model.bpmn_process_id actually has a valid value
        bpmn_process = (
            new_bpmn_process
            or task_model.bpmn_process
            or BpmnProcessModel.query.filter_by(id=task_model.bpmn_process_id).first()
        )

        self.update_task_model(task_model, spiff_task)
        bpmn_process_json_data = self.__class__.update_task_data_on_bpmn_process(
            bpmn_process, spiff_task.workflow.data
        )
        if bpmn_process_json_data is not None:
            self.json_data_dicts[bpmn_process_json_data["hash"]] = bpmn_process_json_data
        self.task_models[task_model.guid] = task_model

        if start_and_end_times:
            task_model.start_in_seconds = start_and_end_times["start_in_seconds"]
            task_model.end_in_seconds = start_and_end_times["end_in_seconds"]

        # let failed tasks raise and we will log the event then
        if task_model.state == "COMPLETED":
            event_type = ProcessInstanceEventType.task_completed.value
            timestamp = task_model.end_in_seconds or task_model.start_in_seconds or time.time()
            (
                process_instance_event,
                _process_instance_error_detail,
            ) = ProcessInstanceTmpService.add_event_to_process_instance(
                self.process_instance,
                event_type,
                task_guid=task_model.guid,
                timestamp=timestamp,
                add_to_db_session=False,
            )
            self.process_instance_events[task_model.guid] = process_instance_event

        self.update_bpmn_process(spiff_task.workflow, bpmn_process)
        return task_model

    def update_bpmn_process(
        self,
        spiff_workflow: BpmnWorkflow,
        bpmn_process: BpmnProcessModel,
    ) -> None:
        new_properties_json = copy.copy(bpmn_process.properties_json)
        new_properties_json["last_task"] = str(spiff_workflow.last_task.id) if spiff_workflow.last_task else None
        new_properties_json["success"] = spiff_workflow.success
        bpmn_process.properties_json = new_properties_json

        bpmn_process_json_data = self.__class__.update_task_data_on_bpmn_process(bpmn_process, spiff_workflow.data)
        if bpmn_process_json_data is not None:
            self.json_data_dicts[bpmn_process_json_data["hash"]] = bpmn_process_json_data

        self.bpmn_processes[bpmn_process.guid or "top_level"] = bpmn_process

        if spiff_workflow.outer_workflow != spiff_workflow:
            direct_parent_bpmn_process = BpmnProcessModel.query.filter_by(
                id=bpmn_process.direct_parent_process_id
            ).first()
            self.update_bpmn_process(spiff_workflow.outer_workflow, direct_parent_bpmn_process)

    def update_task_model(
        self,
        task_model: TaskModel,
        spiff_task: SpiffTask,
    ) -> None:
        """Updates properties_json and data on given task_model.

        This will NOT update start_in_seconds or end_in_seconds.
        It also returns the relating json_data object so they can be imported later.
        """
        new_properties_json = self.serializer.task_to_dict(spiff_task)
        if new_properties_json["task_spec"] == "Start":
            new_properties_json["parent"] = None
        spiff_task_data = new_properties_json.pop("data")
        python_env_data_dict = self.__class__._get_python_env_data_dict_from_spiff_task(spiff_task, self.serializer)
        task_model.properties_json = new_properties_json
        task_model.state = TaskStateNames[new_properties_json["state"]]
        json_data_dict = self.__class__.update_task_data_on_task_model_and_return_dict_if_updated(
            task_model, spiff_task_data, "json_data_hash"
        )
        python_env_dict = self.__class__.update_task_data_on_task_model_and_return_dict_if_updated(
            task_model, python_env_data_dict, "python_env_data_hash"
        )
        if json_data_dict is not None:
            self.json_data_dicts[json_data_dict["hash"]] = json_data_dict
        if python_env_dict is not None:
            self.json_data_dicts[python_env_dict["hash"]] = python_env_dict

    def find_or_create_task_model_from_spiff_task(
        self,
        spiff_task: SpiffTask,
    ) -> Tuple[Optional[BpmnProcessModel], TaskModel]:
        spiff_task_guid = str(spiff_task.id)
        task_model: Optional[TaskModel] = TaskModel.query.filter_by(guid=spiff_task_guid).first()
        bpmn_process = None
        if task_model is None:
            bpmn_process = self.task_bpmn_process(
                spiff_task,
            )
            task_model = TaskModel.query.filter_by(guid=spiff_task_guid).first()
            if task_model is None:
                task_definition = self.bpmn_definition_to_task_definitions_mappings[spiff_task.workflow.spec.name][
                    spiff_task.task_spec.name
                ]
                task_model = TaskModel(
                    guid=spiff_task_guid,
                    bpmn_process_id=bpmn_process.id,
                    process_instance_id=self.process_instance.id,
                    task_definition_id=task_definition.id,
                )
        return (bpmn_process, task_model)

    def task_bpmn_process(
        self,
        spiff_task: SpiffTask,
    ) -> BpmnProcessModel:
        subprocess_guid, subprocess = self.__class__._task_subprocess(spiff_task)
        bpmn_process: Optional[BpmnProcessModel] = None
        if subprocess is None:
            bpmn_process = self.process_instance.bpmn_process
            # This is the top level workflow, which has no guid
            # check for bpmn_process_id because mypy doesn't realize bpmn_process can be None
            if self.process_instance.bpmn_process_id is None:
                spiff_workflow = spiff_task.workflow._get_outermost_workflow()
                bpmn_process = self.add_bpmn_process(
                    bpmn_process_dict=self.serializer.workflow_to_dict(spiff_workflow),
                    spiff_workflow=spiff_workflow,
                )
        else:
            bpmn_process = BpmnProcessModel.query.filter_by(guid=subprocess_guid).first()
            if bpmn_process is None:
                spiff_workflow = spiff_task.workflow
                bpmn_process = self.add_bpmn_process(
                    bpmn_process_dict=self.serializer.workflow_to_dict(subprocess),
                    top_level_process=self.process_instance.bpmn_process,
                    bpmn_process_guid=subprocess_guid,
                    spiff_workflow=spiff_workflow,
                )
        return bpmn_process

    def add_bpmn_process(
        self,
        bpmn_process_dict: dict,
        spiff_workflow: BpmnWorkflow,
        top_level_process: Optional[BpmnProcessModel] = None,
        bpmn_process_guid: Optional[str] = None,
    ) -> BpmnProcessModel:
        """This creates and adds a bpmn_process to the Db session.

        It will also add tasks and relating json_data entries if the bpmn_process is new.
        It returns tasks and json data records in dictionaries to be added to the session later.
        """
        tasks = bpmn_process_dict.pop("tasks")
        bpmn_process_data_dict = bpmn_process_dict.pop("data")

        if "subprocesses" in bpmn_process_dict:
            bpmn_process_dict.pop("subprocesses")
        if "spec" in bpmn_process_dict:
            bpmn_process_dict.pop("spec")
        if "subprocess_specs" in bpmn_process_dict:
            bpmn_process_dict.pop("subprocess_specs")

        bpmn_process = None
        if top_level_process is not None:
            bpmn_process = BpmnProcessModel.query.filter_by(
                top_level_process_id=top_level_process.id, guid=bpmn_process_guid
            ).first()
        elif self.process_instance.bpmn_process_id is not None:
            bpmn_process = self.process_instance.bpmn_process

        bpmn_process_is_new = False
        if bpmn_process is None:
            bpmn_process_is_new = True
            bpmn_process = BpmnProcessModel(guid=bpmn_process_guid)

            bpmn_process_definition = self.bpmn_definition_to_task_definitions_mappings[spiff_workflow.spec.name][
                "bpmn_process_definition"
            ]
            bpmn_process.bpmn_process_definition = bpmn_process_definition

            if top_level_process is not None:
                subprocesses = spiff_workflow._get_outermost_workflow().subprocesses
                direct_bpmn_process_parent = top_level_process
                for subprocess_guid, subprocess in subprocesses.items():
                    if subprocess == spiff_workflow.outer_workflow:
                        direct_bpmn_process_parent = BpmnProcessModel.query.filter_by(
                            guid=str(subprocess_guid)
                        ).first()
                        if direct_bpmn_process_parent is None:
                            raise BpmnProcessNotFoundError(
                                f"Could not find bpmn process with guid: {str(subprocess_guid)} "
                                f"while searching for direct parent process of {bpmn_process_guid}."
                            )

                if direct_bpmn_process_parent is None:
                    raise BpmnProcessNotFoundError(
                        f"Could not find a direct bpmn process parent for guid: {bpmn_process_guid}"
                    )

                bpmn_process.direct_parent_process_id = direct_bpmn_process_parent.id

        # Point the root id to the Start task instead of the Root task
        # since we are ignoring the Root task.
        for task_id, task_properties in tasks.items():
            if task_properties["task_spec"] == "Start":
                bpmn_process_dict["root"] = task_id

        bpmn_process.properties_json = bpmn_process_dict

        bpmn_process_json_data = self.__class__.update_task_data_on_bpmn_process(bpmn_process, bpmn_process_data_dict)
        if bpmn_process_json_data is not None:
            self.json_data_dicts[bpmn_process_json_data["hash"]] = bpmn_process_json_data

        if top_level_process is None:
            self.process_instance.bpmn_process = bpmn_process
        elif bpmn_process.top_level_process_id is None:
            bpmn_process.top_level_process_id = top_level_process.id

        # Since we bulk insert tasks later we need to add the bpmn_process to the session
        # to ensure we have an id.
        db.session.add(bpmn_process)

        if bpmn_process_is_new:
            self.add_tasks_to_bpmn_process(
                tasks=tasks,
                spiff_workflow=spiff_workflow,
                bpmn_process=bpmn_process,
            )
        return bpmn_process

    def add_tasks_to_bpmn_process(
        self,
        tasks: dict,
        spiff_workflow: BpmnWorkflow,
        bpmn_process: BpmnProcessModel,
    ) -> None:
        for task_id, task_properties in tasks.items():
            # The Root task is added to the spec by Spiff when the bpmn process is instantiated
            # within Spiff. We do not actually need it and it's missing from our initial
            # bpmn process defintion so let's avoid using it.
            if task_properties["task_spec"] == "Root":
                continue

            # we are going to avoid saving likely and maybe tasks to the db.
            # that means we need to remove them from their parents' lists of children as well.
            spiff_task = spiff_workflow.get_task_from_id(UUID(task_id))
            if spiff_task._has_state(TaskState.PREDICTED_MASK):
                self.__class__.remove_spiff_task_from_parent(spiff_task, self.task_models)
                continue

            task_model = TaskModel.query.filter_by(guid=task_id).first()
            if task_model is None:
                task_model = self.__class__._create_task(
                    bpmn_process,
                    self.process_instance,
                    spiff_task,
                    self.bpmn_definition_to_task_definitions_mappings,
                )
            self.update_task_model(task_model, spiff_task)
            self.task_models[task_model.guid] = task_model

    @classmethod
    def remove_spiff_task_from_parent(cls, spiff_task: SpiffTask, task_models: dict[str, TaskModel]) -> None:
        """Removes the given spiff task from its parent and then updates the task_models dict with the changes."""
        spiff_task_parent_guid = str(spiff_task.parent.id)
        spiff_task_guid = str(spiff_task.id)
        if spiff_task_parent_guid in task_models:
            parent_task_model = task_models[spiff_task_parent_guid]
            if spiff_task_guid in parent_task_model.properties_json["children"]:
                new_parent_properties_json = copy.copy(parent_task_model.properties_json)
                new_parent_properties_json["children"].remove(spiff_task_guid)
                parent_task_model.properties_json = new_parent_properties_json
                task_models[spiff_task_parent_guid] = parent_task_model

    @classmethod
    def update_task_data_on_bpmn_process(
        cls, bpmn_process: BpmnProcessModel, bpmn_process_data_dict: dict
    ) -> Optional[JsonDataDict]:
        bpmn_process_data_json = json.dumps(bpmn_process_data_dict, sort_keys=True)
        bpmn_process_data_hash: str = sha256(bpmn_process_data_json.encode("utf8")).hexdigest()
        json_data_dict: Optional[JsonDataDict] = None
        if bpmn_process.json_data_hash != bpmn_process_data_hash:
            json_data_dict = {"hash": bpmn_process_data_hash, "data": bpmn_process_data_dict}
            bpmn_process.json_data_hash = bpmn_process_data_hash
        return json_data_dict

    @classmethod
    def insert_or_update_json_data_dict(cls, json_data_dict: JsonDataDict) -> None:
        TaskService.insert_or_update_json_data_records({json_data_dict["hash"]: json_data_dict})

    @classmethod
    def update_task_data_on_task_model_and_return_dict_if_updated(
        cls, task_model: TaskModel, task_data_dict: dict, task_model_data_column: str
    ) -> Optional[JsonDataDict]:
        task_data_json = json.dumps(task_data_dict, sort_keys=True)
        task_data_hash: str = sha256(task_data_json.encode("utf8")).hexdigest()
        json_data_dict: Optional[JsonDataDict] = None
        if getattr(task_model, task_model_data_column) != task_data_hash:
            json_data_dict = {"hash": task_data_hash, "data": task_data_dict}
            setattr(task_model, task_model_data_column, task_data_hash)
        return json_data_dict

    @classmethod
    def bpmn_process_and_descendants(cls, bpmn_processes: list[BpmnProcessModel]) -> list[BpmnProcessModel]:
        bpmn_process_ids = [p.id for p in bpmn_processes]
        direct_children = BpmnProcessModel.query.filter(
            BpmnProcessModel.direct_parent_process_id.in_(bpmn_process_ids)  # type: ignore
        ).all()
        if len(direct_children) > 0:
            return bpmn_processes + cls.bpmn_process_and_descendants(direct_children)
        return bpmn_processes

    @classmethod
    def task_models_of_parent_bpmn_processes(
        cls, task_model: TaskModel, stop_on_first_call_activity: Optional[bool] = False
    ) -> Tuple[list[BpmnProcessModel], list[TaskModel]]:
        """Returns the list of task models that are associated with the parent bpmn process.

        Example: TopLevelProcess has SubprocessTaskA which has CallActivityTaskA which has ScriptTaskA.
        SubprocessTaskA corresponds to SpiffSubprocess1.
        CallActivityTaskA corresponds to SpiffSubprocess2.
        Using ScriptTaskA this will return:
            (
                [TopLevelProcess, SpiffSubprocess1, SpiffSubprocess2],
                [SubprocessTaskA, CallActivityTaskA]
            )

        If stop_on_first_call_activity it will stop when it reaches the first task model with a type of 'CallActivity'.
        This will change the return value in the example to:
            (
                [SpiffSubprocess2],
                [CallActivityTaskA]
            )
        """
        bpmn_process = task_model.bpmn_process
        task_models: list[TaskModel] = []
        bpmn_processes: list[BpmnProcessModel] = [bpmn_process]
        if bpmn_process.guid is not None:
            parent_task_model = TaskModel.query.filter_by(guid=bpmn_process.guid).first()
            task_models.append(parent_task_model)
            if not stop_on_first_call_activity or parent_task_model.task_definition.typename != "CallActivity":
                if parent_task_model is not None:
                    b, t = cls.task_models_of_parent_bpmn_processes(
                        parent_task_model, stop_on_first_call_activity=stop_on_first_call_activity
                    )
                    # order matters here. since we are traversing backwards (from child to parent) then
                    # b and t should be the parents of whatever is in bpmn_processes and task_models.
                    return (b + bpmn_processes, t + task_models)
        return (bpmn_processes, task_models)

    @classmethod
    def full_bpmn_process_path(cls, bpmn_process: BpmnProcessModel) -> list[str]:
        """Returns a list of bpmn process identifiers pointing the given bpmn_process."""
        bpmn_process_identifiers: list[str] = []
        if bpmn_process.guid:
            task_model = TaskModel.query.filter_by(guid=bpmn_process.guid).first()
            (
                parent_bpmn_processes,
                _task_models_of_parent_bpmn_processes,
            ) = TaskService.task_models_of_parent_bpmn_processes(task_model)
            for parent_bpmn_process in parent_bpmn_processes:
                bpmn_process_identifiers.append(parent_bpmn_process.bpmn_process_definition.bpmn_identifier)
        bpmn_process_identifiers.append(bpmn_process.bpmn_process_definition.bpmn_identifier)
        return bpmn_process_identifiers

    @classmethod
    def bpmn_process_for_called_activity_or_top_level_process(cls, task_model: TaskModel) -> BpmnProcessModel:
        """Returns either the bpmn process for the call activity calling the process or the top level bpmn process.

        For example, process_modelA has processA which has a call activity that calls processB which is inside of process_modelB.
        processB has subprocessA which has taskA. Using taskA this method should return processB and then that can be used with
        the spec reference cache to find process_modelB.
        """
        (bpmn_processes, _task_models) = TaskService.task_models_of_parent_bpmn_processes(
            task_model, stop_on_first_call_activity=True
        )
        return bpmn_processes[0]

    @classmethod
    def reset_task_model_dict(
        cls,
        task_model: dict,
        state: str,
    ) -> None:
        task_model["state"] = state
        task_model["start_in_seconds"] = None
        task_model["end_in_seconds"] = None

    @classmethod
    def reset_task_model(
        cls,
        task_model: TaskModel,
        state: str,
        json_data_hash: Optional[str] = None,
        python_env_data_hash: Optional[str] = None,
    ) -> None:
        if json_data_hash is None:
            cls.update_task_data_on_task_model_and_return_dict_if_updated(task_model, {}, "json_data_hash")
        else:
            task_model.json_data_hash = json_data_hash
        if python_env_data_hash is None:
            cls.update_task_data_on_task_model_and_return_dict_if_updated(task_model, {}, "python_env_data")
        else:
            task_model.python_env_data_hash = python_env_data_hash

        task_model.state = state
        task_model.start_in_seconds = None
        task_model.end_in_seconds = None

        new_properties_json = copy.copy(task_model.properties_json)
        new_properties_json["state"] = getattr(TaskState, state)
        task_model.properties_json = new_properties_json

    @classmethod
    def insert_or_update_json_data_records(
        cls, json_data_hash_to_json_data_dict_mapping: dict[str, JsonDataDict]
    ) -> None:
        list_of_dicts = [*json_data_hash_to_json_data_dict_mapping.values()]
        if len(list_of_dicts) > 0:
            on_duplicate_key_stmt = None
            if current_app.config["SPIFFWORKFLOW_BACKEND_DATABASE_TYPE"] == "mysql":
                insert_stmt = mysql_insert(JsonDataModel).values(list_of_dicts)
                on_duplicate_key_stmt = insert_stmt.on_duplicate_key_update(data=insert_stmt.inserted.data)
            else:
                insert_stmt = postgres_insert(JsonDataModel).values(list_of_dicts)
                on_duplicate_key_stmt = insert_stmt.on_conflict_do_nothing(index_elements=["hash"])
            db.session.execute(on_duplicate_key_stmt)

    @classmethod
    def get_extensions_from_task_model(cls, task_model: TaskModel) -> dict:
        task_definition = task_model.task_definition
        extensions: dict = (
            task_definition.properties_json["extensions"] if "extensions" in task_definition.properties_json else {}
        )
        return extensions

    @classmethod
    def get_spec_reference_from_bpmn_process(cls, bpmn_process: BpmnProcessModel) -> SpecReferenceCache:
        """Get the bpmn file for a given task model.

        This involves several queries so avoid calling in a tight loop.
        """
        bpmn_process_definition = bpmn_process.bpmn_process_definition
        spec_reference: Optional[SpecReferenceCache] = SpecReferenceCache.query.filter_by(
            identifier=bpmn_process_definition.bpmn_identifier, type="process"
        ).first()
        if spec_reference is None:
            raise SpecReferenceNotFoundError(
                f"Could not find given process identifier in the cache: {bpmn_process_definition.bpmn_identifier}"
            )
        return spec_reference

    @classmethod
    def get_name_for_display(cls, entity: Union[TaskDefinitionModel, BpmnProcessDefinitionModel]) -> str:
        return entity.bpmn_name or entity.bpmn_identifier

    @classmethod
    def _task_subprocess(cls, spiff_task: SpiffTask) -> Tuple[Optional[str], Optional[BpmnWorkflow]]:
        top_level_workflow = spiff_task.workflow._get_outermost_workflow()
        my_wf = spiff_task.workflow  # This is the workflow the spiff_task is part of
        my_sp = None
        my_sp_id = None
        if my_wf != top_level_workflow:
            # All the subprocesses are at the top level, so you can just compare them
            for sp_id, sp in top_level_workflow.subprocesses.items():
                if sp == my_wf:
                    my_sp = sp
                    my_sp_id = str(sp_id)
                    break
        return (my_sp_id, my_sp)

    @classmethod
    def _create_task(
        cls,
        bpmn_process: BpmnProcessModel,
        process_instance: ProcessInstanceModel,
        spiff_task: SpiffTask,
        bpmn_definition_to_task_definitions_mappings: dict,
    ) -> TaskModel:
        task_definition = bpmn_definition_to_task_definitions_mappings[spiff_task.workflow.spec.name][
            spiff_task.task_spec.name
        ]
        task_model = TaskModel(
            guid=str(spiff_task.id),
            bpmn_process_id=bpmn_process.id,
            process_instance_id=process_instance.id,
            task_definition_id=task_definition.id,
        )
        return task_model

    @classmethod
    def _get_python_env_data_dict_from_spiff_task(
        cls, spiff_task: SpiffTask, serializer: BpmnWorkflowSerializer
    ) -> dict:
        user_defined_state = spiff_task.workflow.script_engine.environment.user_defined_state()
        # this helps to convert items like datetime objects to be json serializable
        converted_data: dict = serializer.data_converter.convert(user_defined_state)
        return converted_data