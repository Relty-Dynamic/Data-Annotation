pipeline {
    agent { label 'built-in' }

    options {
        skipDefaultCheckout(true)
        disableConcurrentBuilds()
        timeout(time: 60, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }

    // Both deployment entrypoints verify the exact feat/s3-mock commit.
    triggers { pollSCM('H/2 * * * *') }

    stages {
        stage('Checkout feat/s3-mock') {
            steps {
                checkout scm
                sh '''#!/bin/sh
                    set -eu
                    test "$(git rev-parse HEAD)" = "$(git rev-parse --verify refs/remotes/origin/feat/s3-mock)"
                '''
            }
        }

        stage('Build and test') {
            steps {
                sh '''#!/bin/sh
                    set -eu
                    tag="$(git rev-parse HEAD)"
                    # Use the HTTPS mirror on this Ubuntu node; leave host pip settings untouched.
                    docker build --build-arg DATAMARK_PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple -f deploy/Dockerfile.web -t "datamark-web:$tag" .
                    docker run --rm --network none \
                        --tmpfs /app/.tmp:rw,uid=1000,gid=1000,mode=0700 \
                        --tmpfs /app/.local:rw,uid=1000,gid=1000,mode=0700 \
                        --mount "type=bind,source=$PWD/launch.py,target=/app/launch.py,readonly" \
                        --mount "type=bind,source=$PWD/deploy/migrate_projects.py,target=/app/deploy/migrate_projects.py,readonly" \
                        "datamark-web:$tag" \
                        /opt/venv/bin/python -c 'import sys, unittest; suite = unittest.defaultTestLoader.discover("backend", pattern="test_*.py"); count = suite.countTestCases(); print(f"Discovered {count} backend tests"); assert count > 0, "backend tests missing from image"; result = unittest.TextTestRunner(verbosity=1).run(suite); sys.exit(not result.wasSuccessful())'
                '''
            }
        }

        stage('Build and test public S3') {
            steps {
                sh '''#!/bin/sh
                    set -eu
                    tag="$(git rev-parse HEAD)"
                    docker build --build-arg "BASE_IMAGE=datamark-web:$tag" \
                        --build-arg DATAMARK_PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
                        -f deploy/Dockerfile.r2-public -t "datamark-r2:$tag" .
                    docker run --rm --network none \
                        --tmpfs /app/.tmp:rw,uid=1000,gid=1000,mode=0700 \
                        --tmpfs /app/.local:rw,uid=1000,gid=1000,mode=0700 \
                        "datamark-r2:$tag" /opt/venv/bin/python -m unittest \
                        backend.test_public_setup backend.test_shared_accounts \
                        backend.test_login_encryption backend.test_r2_integration \
                        backend.test_r2_folder_upload backend.test_tasks_api \
                        backend.test_tasks_store backend.test_public_deployment
                '''
            }
        }

        stage('Deploy NAS and public S3') {
            failFast false
            parallel {
                stage('Deploy feat/s3-mock to intranet 10443') {
                    steps {
                        sh 'bash deploy/ci-deploy-intranet.sh s3-mock-10443'
                    }
                }
                stage('Deploy feat/s3-mock to public S3') {
                    steps {
                        sh 'python3 deploy/ci_deploy_public.py'
                    }
                }
            }
        }

    }
}
