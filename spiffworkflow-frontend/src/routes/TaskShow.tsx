import React, { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import validator from '@rjsf/validator-ajv8';

import {
  TabList,
  Tab,
  Tabs,
  Grid,
  Column,
  ComboBox,
  Button,
  ButtonSet,
} from '@carbon/react';

// eslint-disable-next-line import/no-named-as-default
import Form from '../themes/carbon';
import HttpService from '../services/HttpService';
import useAPIError from '../hooks/UseApiError';
import { modifyProcessIdentifierForPathParam } from '../helpers';
import { EventDefinition, Task } from '../interfaces';
import ProcessBreadcrumb from '../components/ProcessBreadcrumb';
import InstructionsForEndUser from '../components/InstructionsForEndUser';

// TODO: move this somewhere else
function TypeaheadWidget({
  id,
  onChange,
  options: { category, itemFormat },
}: {
  id: string;
  onChange: any;
  options: any;
}) {
  const pathForCategory = (inputText: string) => {
    return `/connector-proxy/typeahead/${category}?prefix=${inputText}&limit=100`;
  };

  const lastSearchTerm = useRef('');
  const [items, setItems] = useState<any[]>([]);
  const [selectedItem, setSelectedItem] = useState<any>(null);
  const itemFormatRegex = /[^{}]+(?=})/g;
  const itemFormatSubstitutions = itemFormat.match(itemFormatRegex);

  const itemToString = (item: any) => {
    if (!item) {
      return null;
    }

    let str = itemFormat;
    itemFormatSubstitutions.forEach((key: string) => {
      str = str.replace(`{${key}}`, item[key]);
    });
    return str;
  };

  const handleTypeAheadResult = (result: any, inputText: string) => {
    if (lastSearchTerm.current === inputText) {
      setItems(result);
    }
  };

  const typeaheadSearch = (inputText: string) => {
    if (inputText) {
      lastSearchTerm.current = inputText;
      // TODO: check cache of prefixes -> results
      HttpService.makeCallToBackend({
        path: pathForCategory(inputText),
        successCallback: (result: any) =>
          handleTypeAheadResult(result, inputText),
      });
    }
  };

  return (
    <ComboBox
      onInputChange={typeaheadSearch}
      onChange={(event: any) => {
        setSelectedItem(event.selectedItem);
        onChange(itemToString(event.selectedItem));
      }}
      id={id}
      items={items}
      itemToString={itemToString}
      placeholder={`Start typing to search for ${category}...`}
      selectedItem={selectedItem}
    />
  );
}

export default function TaskShow() {
  const [task, setTask] = useState<Task | null>(null);
  const [userTasks] = useState(null);
  const params = useParams();
  const navigate = useNavigate();
  const [disabled, setDisabled] = useState(false);
  const [noValidate, setNoValidate] = useState<boolean>(false);

  const [taskData, setTaskData] = useState<any>(null);

  const { addError, removeError } = useAPIError();

  const navigateToInterstitial = (myTask: Task) => {
    navigate(
      `/admin/process-instances/${modifyProcessIdentifierForPathParam(
        myTask.process_model_identifier
      )}/${myTask.process_instance_id}/interstitial`
    );
  };

  useEffect(() => {
    const processResult = (result: Task) => {
      setTask(result);
      setTaskData(result.data);
      setDisabled(false);
      if (!result.can_complete) {
        navigateToInterstitial(result);
      }

      /*  Disable call to load previous tasks -- do not display menu.
      const url = `/v1.0/process-instances/for-me/${modifyProcessIdentifierForPathParam(
        result.process_model_identifier
      )}/${params.process_instance_id}/task-info`;
      // if user is unauthorized to get process-instance task-info then don't do anything
      // Checking like this so we can dynamically create the url with the correct process model
      //  instead of passing the process model identifier in through the params
      HttpService.makeCallToBackend({
        path: url,
        successCallback: (tasks: any) => {
          setDisabled(false);
          setUserTasks(tasks);
        },
        onUnauthorized: () => {
          setDisabled(false);
        },
        failureCallback: (error: any) => {
          addError(error);
        },
      });
      */
    };
    HttpService.makeCallToBackend({
      path: `/tasks/${params.process_instance_id}/${params.task_id}`,
      successCallback: processResult,
      failureCallback: addError,
    });
    // FIXME: not sure what to do about addError. adding it to this array causes the page to endlessly reload
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params]);

  const processSubmitResult = (result: any) => {
    removeError();
    if (result.ok) {
      navigate(`/tasks`);
    } else if (result.process_instance_id) {
      if (result.can_complete) {
        navigate(`/tasks/${result.process_instance_id}/${result.id}`);
      } else {
        navigateToInterstitial(result);
      }
    } else {
      addError(result);
    }
  };

  const handleFormSubmit = (formObject: any, _event: any) => {
    if (disabled) {
      return;
    }

    const dataToSubmit = formObject?.formData;
    if (!dataToSubmit) {
      navigate(`/tasks`);
      return;
    }
    let queryParams = '';

    // if validations are turned off then save as draft
    if (noValidate) {
      queryParams = '?save_as_draft=true';
    }
    setDisabled(true);
    removeError();
    delete dataToSubmit.isManualTask;

    // NOTE: rjsf sets blanks values to undefined and JSON.stringify removes keys with undefined values
    // so there is no way to clear out a field that previously had a value.
    // To resolve this, we could potentially go through the object that we are posting (either in here or in
    // HttpService) and translate all undefined values to null.
    HttpService.makeCallToBackend({
      path: `/tasks/${params.process_instance_id}/${params.task_id}${queryParams}`,
      successCallback: processSubmitResult,
      failureCallback: (error: any) => {
        addError(error);
      },
      httpMethod: 'PUT',
      postBody: dataToSubmit,
    });
  };

  const handleSignalSubmit = (event: EventDefinition) => {
    if (disabled || !task) {
      return;
    }
    HttpService.makeCallToBackend({
      path: `/tasks/${params.process_instance_id}/send-user-signal-event`,
      successCallback: processSubmitResult,
      failureCallback: (error: any) => {
        addError(error);
      },
      httpMethod: 'POST',
      postBody: event,
    });
  };

  const buildTaskNavigation = () => {
    let userTasksElement;
    let selectedTabIndex = 0;
    if (userTasks) {
      userTasksElement = (userTasks as any).map(function getUserTasksElement(
        userTask: any,
        index: number
      ) {
        const taskUrl = `/tasks/${params.process_instance_id}/${userTask.id}`;
        if (userTask.id === params.task_id) {
          selectedTabIndex = index;
          return <Tab selected>{userTask.name_for_display}</Tab>;
        }
        if (userTask.state === 'COMPLETED') {
          return (
            <Tab
              onClick={() => navigate(taskUrl)}
              data-qa={`form-nav-${userTask.name}`}
            >
              {userTask.name_for_display}
            </Tab>
          );
        }
        if (userTask.state === 'FUTURE') {
          return <Tab disabled>{userTask.name_for_display}</Tab>;
        }
        if (userTask.state === 'READY') {
          return (
            <Tab
              onClick={() => navigate(taskUrl)}
              data-qa={`form-nav-${userTask.name}`}
            >
              {userTask.name_for_display}
            </Tab>
          );
        }
        return null;
      });
      return (
        <Tabs
          title="Steps in this process instance involving people"
          selectedIndex={selectedTabIndex}
        >
          <TabList aria-label="List of tabs" contained>
            {userTasksElement}
          </TabList>
        </Tabs>
      );
    }
    return null;
  };

  const getFieldsWithDateValidations = (
    jsonSchema: any,
    formData: any,
    errors: any
  ) => {
    if ('properties' in jsonSchema) {
      Object.keys(jsonSchema.properties).forEach((propertyKey: string) => {
        const propertyMetadata = jsonSchema.properties[propertyKey];
        if (
          typeof propertyMetadata === 'object' &&
          'minimumDate' in propertyMetadata &&
          propertyMetadata.minimumDate === 'today'
        ) {
          const dateToday = new Date();
          const dateValue = formData[propertyKey];
          if (dateValue) {
            const dateValueObject = new Date(dateValue);
            const dateValueString = dateValueObject.toISOString().split('T')[0];
            const dateTodayString = dateToday.toISOString().split('T')[0];
            if (dateTodayString > dateValueString) {
              errors[propertyKey].addError('must be today or after');
            }
          }
        }

        // recurse through all nested properties as well
        getFieldsWithDateValidations(
          propertyMetadata,
          formData[propertyKey],
          errors[propertyKey]
        );
      });
    }
    return errors;
  };

  // This turns off validations and then dispatches the click event after
  // waiting a second to give the state time to update.
  // This is to allow saving the form without validations causing issues.
  const handleSaveAndCloseButton = () => {
    setNoValidate(true);
    setTimeout(() => {
      (document.getElementById('our-very-own-form') as any).dispatchEvent(
        new Event('submit', { cancelable: true, bubbles: true })
      );
    }, 1000);
  };

  const formElement = () => {
    if (!task) {
      return null;
    }

    let formUiSchema;
    let jsonSchema = task.form_schema;
    let reactFragmentToHideSubmitButton = null;
    if (task.typename === 'ManualTask') {
      jsonSchema = {
        type: 'object',
        required: [],
        properties: {
          isManualTask: {
            type: 'boolean',
            title: 'Is ManualTask',
            default: true,
          },
        },
      };
      formUiSchema = {
        isManualTask: {
          'ui:widget': 'hidden',
        },
      };
    } else if (task.form_ui_schema) {
      formUiSchema = task.form_ui_schema;
    }
    if (task.state !== 'READY') {
      formUiSchema = Object.assign(formUiSchema || {}, {
        'ui:readonly': true,
      });

      // It doesn't seem as if Form allows for removing the default submit button
      // so passing a blank fragment or children seem to do it though
      //
      // from: https://github.com/rjsf-team/react-jsonschema-form/issues/1602
      reactFragmentToHideSubmitButton = <div />;
    }

    if (task.state === 'READY') {
      let submitButtonText = 'Submit';
      let closeButton = null;
      if (task.typename === 'ManualTask') {
        submitButtonText = 'Continue';
      } else if (task.typename === 'UserTask') {
        closeButton = (
          <Button
            id="close-button"
            onClick={handleSaveAndCloseButton}
            disabled={disabled}
            kind="secondary"
            title="Save changes without submitting."
          >
            Save and Close
          </Button>
        );
      }
      reactFragmentToHideSubmitButton = (
        <ButtonSet>
          <Button type="submit" id="submit-button" disabled={disabled}>
            {submitButtonText}
          </Button>
          {closeButton}
          <>
            {task.signal_buttons.map((signal) => (
              <Button
                name="signal.signal"
                disabled={disabled}
                onClick={() => handleSignalSubmit(signal.event)}
              >
                {signal.label}
              </Button>
            ))}
          </>
        </ButtonSet>
      );
    }

    const customValidate = (formData: any, errors: any) => {
      return getFieldsWithDateValidations(jsonSchema, formData, errors);
    };

    const widgets = { typeahead: TypeaheadWidget };

    return (
      <Grid fullWidth condensed>
        <Column sm={4} md={5} lg={8}>
          <Form
            id="our-very-own-form"
            disabled={disabled}
            formData={taskData}
            onChange={(obj: any) => setTaskData(obj.formData)}
            onSubmit={handleFormSubmit}
            schema={jsonSchema}
            uiSchema={formUiSchema}
            widgets={widgets}
            validator={validator}
            customValidate={customValidate}
            noValidate={noValidate}
            omitExtraData
          >
            {reactFragmentToHideSubmitButton}
          </Form>
        </Column>
      </Grid>
    );
  };

  if (task) {
    let statusString = '';
    if (task.state !== 'READY') {
      statusString = ` ${task.state}`;
    }

    return (
      <main>
        <ProcessBreadcrumb
          hotCrumbs={[
            [
              `Process Instance Id: ${params.process_instance_id}`,
              `/admin/process-instances/for-me/${modifyProcessIdentifierForPathParam(
                task.process_model_identifier
              )}/${params.process_instance_id}`,
            ],
            [`Task: ${task.name_for_display || task.id}`],
          ]}
        />
        <div>{buildTaskNavigation()}</div>
        <h3>
          Task: {task.name_for_display} ({task.process_model_display_name})
          {statusString}
        </h3>
        <InstructionsForEndUser task={task} />
        {formElement()}
      </main>
    );
  }

  return null;
}
