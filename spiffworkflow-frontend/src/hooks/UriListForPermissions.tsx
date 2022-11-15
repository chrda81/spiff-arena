import { useParams } from 'react-router-dom';

export const useUriListForPermissions = () => {
  const params = useParams();
  const targetUris = {
    processGroupListPath: `/v1.0/process-groups`,
    processGroupShowPath: `/v1.0/process-groups/${params.process_group_id}`,
    processModelCreatePath: `/v1.0/process-models/${params.process_group_id}`,
    processModelShowPath: `/v1.0/process-models/${params.process_model_id}`,
    processModelFileCreatePath: `/v1.0/process-models/${params.process_model_id}/files`,
    processModelFileShowPath: `/v1.0/process-models/${params.process_model_id}/files/${params.file_name}`,
    processInstanceListPath: `/v1.0/process-instances`,
    processInstanceActionPath: `/v1.0/process-models/${params.process_model_id}/process-instances`,
  };

  return { targetUris };
};