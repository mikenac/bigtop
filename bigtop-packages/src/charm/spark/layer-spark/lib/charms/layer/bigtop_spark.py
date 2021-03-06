# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import time
from jujubigdata import utils
from path import Path

from charms.layer.apache_bigtop_base import Bigtop
from charms import layer
from charmhelpers.core import hookenv, host, unitdata
from charmhelpers.fetch.archiveurl import ArchiveUrlFetchHandler


class Spark(object):
    """
    This class manages Spark.
    """
    def __init__(self):
        self.dist_config = utils.DistConfig(
            data=layer.options('hadoop-client'))

    # translate our execution_mode into the appropriate --master value
    def get_master_url(self, spark_master_host):
        mode = hookenv.config()['spark_execution_mode']
        zk_units = unitdata.kv().get('zookeeper.units', [])
        master = None
        if mode.startswith('local') or mode == 'yarn-cluster':
            master = mode
        elif mode == 'standalone' and not zk_units:
            master = 'spark://{}:7077'.format(spark_master_host)
        elif mode == 'standalone' and zk_units:
            master_ips = [p[1] for p in unitdata.kv().get('sparkpeer.units')]
            nodes = []
            for ip in master_ips:
                nodes.append('{}:7077'.format(ip))
            nodes_str = ','.join(nodes)
            master = 'spark://{}'.format(nodes_str)
        elif mode.startswith('yarn'):
            master = 'yarn-client'
        return master

    def install_benchmark(self):
        """
        Install and configure SparkBench.

        If config[spark_bench_enabled], fetch, install, and configure
        SparkBench on initial invocation. Subsequent invocations will skip the
        fetch/install, but will reconfigure SparkBench since we may need to
        adjust the data dir (eg: benchmark data is stored in hdfs when spark
        is in yarn mode; locally in all other execution modes).
        """
        install_sb = hookenv.config()['spark_bench_enabled']
        sb_dir = '/home/ubuntu/SparkBench'
        if install_sb:
            # Fetch/install on our first go-round, then set unit data so we
            # don't reinstall every time this function is called.
            if not unitdata.kv().get('spark_bench.installed', False):
                if utils.cpu_arch() == 'ppc64le':
                    sb_url = hookenv.config()['spark_bench_ppc64le']
                else:
                    # TODO: may need more arch cases (go with x86 sb for now)
                    sb_url = hookenv.config()['spark_bench_x86_64']

                Path(sb_dir).rmtree_p()
                au = ArchiveUrlFetchHandler()
                au.install(sb_url, '/home/ubuntu')

                # NB: This block is unused when using one of our sb tgzs. It
                # may come in handy if people want a tgz that does not expand
                # to our expected sb_dir.
                # #####
                # Handle glob if we use a .tgz that doesn't expand to sb_dir
                # sb_archive_dir = glob('/home/ubuntu/SparkBench*')[0]
                # SparkBench expects to live in ~/SparkBench, so put it there
                # Path(sb_archive_dir).rename(sb_dir)
                # #####

                # Ensure users in the spark group can write to any subdirectory
                # of sb_dir (spark needs to write benchmark output there when
                # running in local modes).
                host.chownr(Path(sb_dir), 'ubuntu', 'spark', chowntopdir=True)
                for r, d, f in os.walk(sb_dir):
                    os.chmod(r, 0o2775)

                unitdata.kv().set('spark_bench.installed', True)
                unitdata.kv().flush(True)

            # Configure the SB env every time this function is called.
            sb_conf = '{}/conf'.format(sb_dir)
            sb_env = Path(sb_conf) / 'env.sh'
            if not sb_env.exists():
                (Path(sb_conf) / 'env.sh.template').copy(sb_env)

            # NB: A few notes on configuring SparkBench:
            # 1. Input data has been pregenerated and packed into the tgz. All
            # spark cluster members will have this data locally, which enables
            # us to execute benchmarks in the absense of HDFS. When spark is in
            # yarn mode, we'll need to generate and store this data in HDFS
            # so nodemanagers can access it (NMs obviously won't have SB
            # installed locally). Set DATA_HDFS to a local dir or common HDFS
            # location depending on our spark execution mode.
            #
            # 2. SB tries to SSH to spark workers to purge vmem caches. This
            # isn't possible in containers, nor is it possible in our env
            # because we don't distribute ssh keys among cluster members.
            # Set MC_LIST to an empty string to prevent this behavior.
            #
            # 3. Throughout SB, HADOOP_HOME/bin is used as the prefix for the
            # hdfs command. Bigtop's hdfs lives at /usr/bin/hdfs, so set the
            # SB HADOOP_HOME accordingly (it's not used for anything else).
            #
            # 4. Use our MASTER envar to set the SparkBench SPARK_MASTER url.
            # It is updated every time we (re)configure spark.
            mode = hookenv.config()['spark_execution_mode']
            if mode.startswith('yarn'):
                sb_data_dir = "hdfs:///user/ubuntu/SparkBench"
            else:
                sb_data_dir = "file://{}".format(sb_dir)

            utils.re_edit_in_place(sb_env, {
                r'^DATA_HDFS *=.*': 'DATA_HDFS="{}"'.format(sb_data_dir),
                r'^DATASET_DIR *=.*': 'DATASET_DIR="{}/dataset"'.format(sb_dir),
                r'^MC_LIST *=.*': 'MC_LIST=""',
                r'.*HADOOP_HOME *=.*': 'HADOOP_HOME="/usr"',
                r'.*SPARK_HOME *=.*': 'SPARK_HOME="/usr/lib/spark"',
                r'^SPARK_MASTER *=.*': 'SPARK_MASTER="$MASTER"',
            })
        else:
            # config[spark_bench_enabled] is false; remove it
            Path(sb_dir).rmtree_p()
            unitdata.kv().set('spark_bench.installed', False)
            unitdata.kv().flush(True)

    def setup(self):
        self.dist_config.add_users()
        self.dist_config.add_dirs()
        self.install_demo()

    def setup_hdfs_logs(self):
        # create hdfs storage space for history server
        dc = self.dist_config
        events_dir = dc.path('spark_events')
        events_dir = 'hdfs://{}'.format(events_dir)
        utils.run_as('hdfs', 'hdfs', 'dfs', '-mkdir', '-p', events_dir)
        utils.run_as('hdfs', 'hdfs', 'dfs', '-chmod', '1777', events_dir)
        utils.run_as('hdfs', 'hdfs', 'dfs', '-chown', '-R', 'ubuntu:spark',
                     events_dir)
        return events_dir

    def configure(self, available_hosts, zk_units, peers):
        """
        This is the core logic of setting up spark.

        Two flags are needed:

          * Namenode exists aka HDFS is ready
          * Resource manager exists aka YARN is ready

        both flags are infered from the available hosts.

        :param dict available_hosts: Hosts that Spark should know about.
        """
        # Bootstrap spark
        if not unitdata.kv().get('spark.bootstrapped', False):
            self.setup()
            unitdata.kv().set('spark.bootstrapped', True)

        # Set KV based on connected applications
        unitdata.kv().set('zookeeper.units', zk_units)
        unitdata.kv().set('sparkpeer.units', peers)
        unitdata.kv().flush(True)

        # Get our config ready
        dc = self.dist_config
        events_log_dir = 'file://{}'.format(dc.path('spark_events'))
        mode = hookenv.config()['spark_execution_mode']
        master_ip = utils.resolve_private_address(available_hosts['spark-master'])
        master_url = self.get_master_url(master_ip)

        # Setup hosts dict
        hosts = {
            'spark': master_ip,
        }
        if 'namenode' in available_hosts:
            hosts['namenode'] = available_hosts['namenode']
            events_log_dir = self.setup_hdfs_logs()

        if 'resourcemanager' in available_hosts:
            hosts['resourcemanager'] = available_hosts['resourcemanager']

        # Setup roles dict. We always include the history server and client.
        # Determine other roles based on our execution mode.
        roles = ['spark-history-server', 'spark-client']
        if mode == 'standalone':
            roles.append('spark-master')
            roles.append('spark-worker')
        elif mode.startswith('yarn'):
            roles.append('spark-on-yarn')
            roles.append('spark-yarn-slave')

        # Setup overrides dict
        override = {
            'spark::common::master_url': master_url,
            'spark::common::event_log_dir': events_log_dir,
            'spark::common::history_log_dir': events_log_dir,
        }
        if zk_units:
            zks = []
            for unit in zk_units:
                ip = utils.resolve_private_address(unit['host'])
                zks.append("%s:%s" % (ip, unit['port']))

            zk_connect = ",".join(zks)
            override['spark::common::zookeeper_connection_string'] = zk_connect
        else:
            override['spark::common::zookeeper_connection_string'] = None

        # Create our site.yaml and trigger puppet
        bigtop = Bigtop()
        bigtop.render_site_yaml(hosts, roles, override)
        bigtop.trigger_puppet()

        # Do this after our puppet bits in case puppet overrides needed perms
        if 'namenode' not in available_hosts:
            # Local event dir (not in HDFS) needs to be 777 so non-spark
            # users can write job history there. It needs to be g+s so
            # all entries will be readable by spark (in the spark group).
            # It needs to be +t so users cannot remove files they don't own.
            dc.path('spark_events').chmod(0o3777)

        self.patch_worker_master_url(master_ip, master_url)

        # handle tuning options that may be set as percentages
        driver_mem = '1g'
        req_driver_mem = hookenv.config()['driver_memory']
        executor_mem = '1g'
        req_executor_mem = hookenv.config()['executor_memory']
        if req_driver_mem.endswith('%'):
            if mode == 'standalone' or mode.startswith('local'):
                mem_mb = host.get_total_ram() / 1024 / 1024
                req_percentage = float(req_driver_mem.strip('%')) / 100
                driver_mem = str(int(mem_mb * req_percentage)) + 'm'
            else:
                hookenv.log("driver_memory percentage in non-local mode. Using 1g default.",
                            level=None)
        else:
            driver_mem = req_driver_mem

        if req_executor_mem.endswith('%'):
            if mode == 'standalone' or mode.startswith('local'):
                mem_mb = host.get_total_ram() / 1024 / 1024
                req_percentage = float(req_executor_mem.strip('%')) / 100
                executor_mem = str(int(mem_mb * req_percentage)) + 'm'
            else:
                hookenv.log("executor_memory percentage in non-local mode. Using 1g default.",
                            level=None)
        else:
            executor_mem = req_executor_mem

        spark_env = '/etc/spark/conf/spark-env.sh'
        utils.re_edit_in_place(spark_env, {
            r'.*SPARK_DRIVER_MEMORY.*': 'export SPARK_DRIVER_MEMORY={}'.format(driver_mem),
            r'.*SPARK_EXECUTOR_MEMORY.*': 'export SPARK_EXECUTOR_MEMORY={}'.format(executor_mem),
        }, append_non_matches=True)

        # Install SB (subsequent calls will reconfigure existing install)
        # SparkBench looks for the spark master in /etc/environment
        with utils.environment_edit_in_place('/etc/environment') as env:
            env['MASTER'] = master_url
        self.install_benchmark()

    def patch_worker_master_url(self, master_ip, master_url):
        '''
        Patch the worker startup script to use the full master url istead of contracting it.
        The master url is placed in the spark-env.sh so that the startup script will use it.
        In HA mode the master_ip is set to be the local_ip instead of the one the leader
        elects. This requires a restart of the master service.
        '''
        zk_units = unitdata.kv().get('zookeeper.units', [])
        if master_url.startswith('spark://'):
            if zk_units:
                master_ip = hookenv.unit_private_ip()
            spark_env = '/etc/spark/conf/spark-env.sh'
            utils.re_edit_in_place(spark_env, {
                r'.*SPARK_MASTER_URL.*': "export SPARK_MASTER_URL={}".format(master_url),
                r'.*SPARK_MASTER_IP.*': "export SPARK_MASTER_IP={}".format(master_ip),
            }, append_non_matches=True)

            self.inplace_change('/etc/init.d/spark-worker',
                                'spark://$SPARK_MASTER_IP:$SPARK_MASTER_PORT',
                                '$SPARK_MASTER_URL')

    def inplace_change(self, filename, old_string, new_string):
        # Safely read the input filename using 'with'
        with open(filename) as f:
            s = f.read()
            if old_string not in s:
                return

        # Safely write the changed content, if found in the file
        with open(filename, 'w') as f:
            s = s.replace(old_string, new_string)
            f.write(s)

    def install_demo(self):
        '''
        Install sparkpi.sh to /home/ubuntu (executes SparkPI example app)
        '''
        demo_source = 'scripts/sparkpi.sh'
        demo_target = '/home/ubuntu/sparkpi.sh'
        Path(demo_source).copy(demo_target)
        Path(demo_target).chmod(0o755)
        Path(demo_target).chown('ubuntu', 'hadoop')

    def start(self):
        '''
        Always start the Spark History Server. Start other services as
        required by our execution mode. Open related ports as appropriate.
        '''
        host.service_start('spark-history-server')
        hookenv.open_port(self.dist_config.port('spark-history-ui'))

        # Spark master/worker is only started in standalone mode
        if hookenv.config()['spark_execution_mode'] == 'standalone':
            if host.service_start('spark-master'):
                hookenv.log("Spark Master started")
                hookenv.open_port(self.dist_config.port('spark-master-ui'))
                # If the master started and we have peers, wait 2m for recovery
                # before starting the worker. This ensures the worker binds
                # to the correct master.
                if unitdata.kv().get('sparkpeer.units'):
                    hookenv.status_set('maintenance',
                                       'waiting for spark master recovery')
                    hookenv.log("Waiting 2m to ensure spark master is ALIVE")
                    time.sleep(120)
            else:
                hookenv.log("Spark Master did not start; this is normal "
                            "for non-leader units in standalone mode")

            # NB: Start the worker even if the master process on this unit
            # fails to start. In non-HA mode, spark master only runs on the
            # leader. On non-leader units, we still want a worker bound to
            # the leader.
            if host.service_start('spark-worker'):
                hookenv.log("Spark Worker started")
                hookenv.open_port(self.dist_config.port('spark-worker-ui'))
            else:
                hookenv.log("Spark Worker did not start")

    def stop(self):
        '''
        Stop all services (and close associated ports). Stopping a service
        that is not currently running does no harm.
        '''
        host.service_stop('spark-history-server')
        hookenv.close_port(self.dist_config.port('spark-history-ui'))

        # Stop the worker before the master
        host.service_stop('spark-worker')
        hookenv.close_port(self.dist_config.port('spark-worker-ui'))
        host.service_stop('spark-master')
        hookenv.close_port(self.dist_config.port('spark-master-ui'))
