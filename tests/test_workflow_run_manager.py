# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2019, 2020, 2021, 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA-Workflow-Controller WorkflowRunManager tests."""

from __future__ import absolute_import, print_function

import pytest
from kubernetes.client.rest import ApiException
from mock import DEFAULT, Mock, patch

from reana_commons.config import KRB5_INIT_CONTAINER_NAME
from reana_db.models import RunStatus, InteractiveSession, InteractiveSessionType

from reana_workflow_controller.errors import REANAInteractiveSessionError
from reana_workflow_controller.workflow_run_manager import KubernetesWorkflowRunManager


def test_start_interactive_session(sample_serial_workflow_in_db):
    """Test interactive workflow run deployment."""
    with patch.multiple(
        "reana_workflow_controller.k8s",
        current_k8s_corev1_api_client=DEFAULT,
        current_k8s_networking_api_client=DEFAULT,
        current_k8s_appsv1_api_client=DEFAULT,
    ) as mocks:
        kwrm = KubernetesWorkflowRunManager(sample_serial_workflow_in_db)
        if len(InteractiveSessionType):
            kwrm.start_interactive_session(InteractiveSessionType(0).name)
        mocks[
            "current_k8s_appsv1_api_client"
        ].create_namespaced_deployment.assert_called_once()
        mocks[
            "current_k8s_corev1_api_client"
        ].create_namespaced_service.assert_called_once()
        mocks[
            "current_k8s_networking_api_client"
        ].create_namespaced_ingress.assert_called_once()


def test_start_interactive_workflow_k8s_failure(sample_serial_workflow_in_db):
    """Test failure of an interactive workflow run deployment because of ."""
    mocked_k8s_client = Mock()
    mocked_k8s_client.create_namespaced_deployment = Mock(
        side_effect=ApiException(reason="some reason")
    )
    with patch.multiple(
        "reana_workflow_controller.k8s",
        current_k8s_appsv1_api_client=mocked_k8s_client,
        current_k8s_corev1_api_client=DEFAULT,
        current_k8s_networking_api_client=DEFAULT,
    ):
        with pytest.raises(
            REANAInteractiveSessionError, match=r".*Kubernetes has failed.*"
        ):
            kwrm = KubernetesWorkflowRunManager(sample_serial_workflow_in_db)
            if len(InteractiveSessionType):
                kwrm.start_interactive_session(InteractiveSessionType(0).name)


def test_atomic_creation_of_interactive_session(sample_serial_workflow_in_db):
    """Test atomic creation of interactive sessions.

    All interactive session should be created as well as writing the state
    to DB, either all should be done or nothing.
    """
    mocked_k8s_client = Mock()
    mocked_k8s_client.create_namespaced_deployment = Mock(
        side_effect=ApiException(reason="Error while creating deployment")
    )
    # Raise 404 when deleting Deployment, because it doesn't exist
    mocked_k8s_client.delete_namespaced_deployment = Mock(
        side_effect=ApiException(reason="Not Found")
    )
    with patch.multiple(
        "reana_workflow_controller.k8s",
        current_k8s_appsv1_api_client=mocked_k8s_client,
        current_k8s_networking_api_client=DEFAULT,
        current_k8s_corev1_api_client=DEFAULT,
    ) as mocks:
        try:
            kwrm = KubernetesWorkflowRunManager(sample_serial_workflow_in_db)
            if len(InteractiveSessionType):
                kwrm.start_interactive_session(InteractiveSessionType(0).name)
        except REANAInteractiveSessionError:
            mocks[
                "current_k8s_corev1_api_client"
            ].delete_namespaced_service.assert_called_once()
            mocks[
                "current_k8s_networking_api_client"
            ].delete_namespaced_ingress.assert_called_once()
            mocked_k8s_client.delete_namespaced_deployment.assert_called_once()
            assert not sample_serial_workflow_in_db.sessions.all()


def test_stop_workflow_backend_only_kubernetes(
    sample_serial_workflow_in_db, add_kubernetes_jobs_to_workflow
):
    """Test deletion of workflows with only Kubernetes based jobs."""
    workflow = sample_serial_workflow_in_db
    workflow.status = RunStatus.running
    workflow_jobs = add_kubernetes_jobs_to_workflow(workflow)
    backend_job_ids = [job.backend_job_id for job in workflow_jobs]
    with patch(
        "reana_workflow_controller.workflow_run_manager."
        "current_k8s_batchv1_api_client"
    ) as api_client:
        kwrm = KubernetesWorkflowRunManager(workflow)
        kwrm.stop_batch_workflow_run()
        for delete_call in api_client.delete_namespaced_job.call_args_list:
            if delete_call.args[0] in backend_job_ids:
                del backend_job_ids[backend_job_ids.index(delete_call.args[0])]

        assert not backend_job_ids


def test_interactive_session_closure(sample_serial_workflow_in_db, session):
    """Test closure of an interactive sessions."""
    mocked_k8s_client = Mock()
    workflow = sample_serial_workflow_in_db
    with patch.multiple(
        "reana_workflow_controller.k8s",
        current_k8s_appsv1_api_client=mocked_k8s_client,
        current_k8s_networking_api_client=DEFAULT,
        current_k8s_corev1_api_client=DEFAULT,
    ):
        kwrm = KubernetesWorkflowRunManager(workflow)
        if len(InteractiveSessionType):
            kwrm.start_interactive_session(InteractiveSessionType(0).name)

            int_session = InteractiveSession.query.filter_by(
                owner_id=workflow.owner_id,
                type_=InteractiveSessionType(0).name,
            ).first()
            assert int_session.status == RunStatus.created
            kwrm.stop_interactive_session(int_session.id_)
            assert not workflow.sessions.first()


def test_create_job_spec_kerberos(
    sample_serial_workflow_in_db,
    kerberos_user_secrets,
    corev1_api_client_with_user_secrets,
):
    """Test creation of k8s job specification when Kerberos is required."""
    workflow = sample_serial_workflow_in_db
    workflow.reana_specification["workflow"].setdefault("resources", {})[
        "kerberos"
    ] = True

    with patch(
        "reana_commons.k8s.secrets.current_k8s_corev1_api_client",
        corev1_api_client_with_user_secrets(kerberos_user_secrets),
    ):
        kwrm = KubernetesWorkflowRunManager(workflow)
        job = kwrm._create_job_spec("run-batch-test")

    init_containers = job.spec.template.spec.init_containers
    assert len(init_containers) == 1
    assert init_containers[0]["name"] == KRB5_INIT_CONTAINER_NAME

    volumes = [volume["name"] for volume in job.spec.template.spec.volumes]
    assert len(set(volumes)) == len(volumes)  # volumes have unique names
    assert any(volume.startswith("reana-secretsstore") for volume in volumes)
    assert "krb5-cache" in volumes
    assert "krb5-conf" in volumes
