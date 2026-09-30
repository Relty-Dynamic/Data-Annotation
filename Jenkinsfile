pipeline {
    agent { label 'built-in' }

    options {
        skipDefaultCheckout(true)
        disableConcurrentBuilds()
        timeout(time: 60, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }

    // Poll main every two minutes; a build runs only when the commit changes.
    triggers { pollSCM('H/2 * * * *') }

    stages {
        stage('Checkout main') {
            steps {
                checkout scm
                sh '''#!/bin/sh
                    set -eu
                    test "$(git rev-parse HEAD)" = "$(git rev-parse refs/remotes/origin/main)"
                '''
            }
        }

        stage('Build and test') {
            steps {
                sh '''#!/bin/sh
                    set -eu
                    tag="$(git rev-parse HEAD)"
                    docker build -f deploy/Dockerfile.web -t "datamark-web:$tag" .
                    docker run --rm --network none "datamark-web:$tag" \
                        /opt/venv/bin/python -c 'import sys, unittest; suite = unittest.defaultTestLoader.discover("backend", pattern="test_*.py"); count = suite.countTestCases(); print(f"Discovered {count} backend tests"); assert count > 0, "backend tests missing from image"; result = unittest.TextTestRunner(verbosity=1).run(suite); sys.exit(not result.wasSuccessful())'
                '''
            }
        }

        stage('Deploy to relty-server') {
            steps {
                sh 'bash deploy/ci-deploy-intranet.sh'
            }
        }
    }
}
