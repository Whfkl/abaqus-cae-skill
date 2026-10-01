"""Explicit real-CAE integration check, run against a dedicated empty session.

This creates a small truss only as a transport/API test. It is not Skill content
or an engineering workflow. The caller starts CAE and supplies its exact session.
"""

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config-dir', required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--workdir', required=True)
    args = parser.parse_args()
    directory = Path(args.workdir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    events = []
    prefix = [sys.executable, '-m', 'abaqus_cae_skill', '--config-dir', args.config_dir,
              '--session', args.session, '--timeout', '30']

    def call(*arguments, stdin=None, expect=0):
        response = subprocess.run(prefix + list(arguments), input=stdin, encoding='utf-8',
                                  capture_output=True, timeout=45)
        data = json.loads(response.stdout)
        events.append({'command': list(arguments), 'exitCode': response.returncode, 'response': data})
        if response.returncode != expect:
            raise RuntimeError(str(data))
        return data

    try:
        session = call('ping')['session']
        if session.get('models') != ['Model-1']:
            raise ValueError('Use a dedicated, empty CAE session for this integration test')
        call('set-workdir', str(directory))
        source = directory / '协同建模 smoke.py'
        (directory / 'smoke_values.py').write_text('young = 210e9\n', encoding='utf-8')
        source.write_text("""from abaqus import mdb, session
from abaqusConstants import *
import mesh, os
from smoke_values import young
if __name__ == '__main__':
    model = mdb.Model(name='SkillSmoke')
    part = model.Part(name='Bar', dimensionality=THREE_D, type=DEFORMABLE_BODY)
    part.WirePolyLine(points=(((0., 0., 0.), (1., 0., 0.)),), mergeType=IMPRINT, meshable=ON)
    material = model.Material(name='Material')
    material.Elastic(table=((young, 0.3),))
    model.TrussSection(name='Section', material='Material', area=0.01)
    part.Set(name='All', edges=part.edges)
    part.SectionAssignment(region=part.sets['All'], sectionName='Section')
    part.setElementType(regions=(part.edges,), elemTypes=(mesh.ElemType(elemCode=T3D2, elemLibrary=STANDARD),))
    part.seedPart(size=0.25)
    part.generateMesh()
    assembly = model.rootAssembly
    instance = assembly.Instance(name='Bar-1', part=part, dependent=ON)
    assembly.Set(name='Lateral', nodes=instance.nodes)
    assembly.Set(name='Fixed', nodes=instance.nodes.getByBoundingBox(xMin=-1e-8, xMax=1e-8))
    assembly.Set(name='Loaded', nodes=instance.nodes.getByBoundingBox(xMin=1.-1e-8, xMax=1.+1e-8))
    model.DisplacementBC(name='Lateral', createStepName='Initial', region=assembly.sets['Lateral'], u2=0., u3=0.)
    model.DisplacementBC(name='Fixed', createStepName='Initial', region=assembly.sets['Fixed'], u1=0.)
    model.StaticStep(name='Load', previous='Initial')
    model.ConcentratedForce(name='Force', createStepName='Load', region=assembly.sets['Loaded'], cf1=1000.)
    session.viewports[session.currentViewportName].setValues(displayedObject=part)
    result = {'sourceFile': __file__, 'model': model.name, 'young': young, 'nodes': len(part.nodes)}
""", encoding='utf-8')
        file_result = call('run-python', '--file', str(source))['value']
        if file_result['sourceFile'] != str(source) or file_result['nodes'] < 2:
            raise ValueError('File context or mesh creation failed')
        changed = call('run-python', '--code',
                       "mdb.models['SkillSmoke'].materials['Material'].elastic.setValues(table=((200e9, 0.3),))\nresult = mdb.models['SkillSmoke'].materials['Material'].elastic.table[0][0]")
        if changed['value'] != 200e9:
            raise ValueError('Collaborative material edit did not persist')
        stdin_result = call('run-python', '--stdin', stdin="result = {'models': list(mdb.models.keys()), 'sourceFileLeaked': '__file__' in globals()}\n")
        if stdin_result['value']['sourceFileLeaked']:
            raise ValueError('Script context leaked into inline execution')
        bad = directory / 'error_location.py'
        bad.write_text("raise ValueError('expected live smoke error')\n", encoding='utf-8')
        failure = call('run-python', '--file', str(bad), expect=1)
        if failure['error']['at']['file'] != str(bad):
            raise ValueError('Error did not preserve script filename')
        call('run-python', '--code',
             "import os\nmdb.saveAs(pathName=os.path.join(os.getcwd(), 'smoke.cae'))\njob = mdb.Job(name='SkillSmoke', model='SkillSmoke', numCpus=1)\njob.writeInput()\njob.submit()\nresult = {'job': job.name}")
        deadline = time.time() + 90
        while time.time() < deadline:
            job = call('monitor-job-status', '--job-name', 'SkillSmoke')
            if job['terminal']:
                break
            time.sleep(1)
        if job['state'] != 'COMPLETED':
            raise ValueError(f"Solve did not complete: {job}")
        odb_path = directory / 'SkillSmoke.odb'
        field = call('inspect-odb', str(odb_path), '--variable', 'U', '--component', 'U1')['fieldSummary']
        expected = 1000. / (200e9 * .01)
        if not math.isclose(field['max'], expected, rel_tol=1e-5):
            raise ValueError(f"Displacement mismatch: {field['max']} vs {expected}")
        image = call('capture-viewport', '--out', str(directory / 'viewport.png'))
        if not Path(image['savedPath']).is_file() or image['sizeBytes'] < 100:
            raise ValueError('No image artifact')
        report = {'ok': True, 'kernel': session['executable'], 'guiThread': session['guiProcess']['thread'],
                  'sessionId': args.session, 'displacement': field['max'], 'expected': expected,
                  'relativeError': abs(field['max'] - expected) / expected,
                  'checks': ['automatic startup (session selected)', 'ping', 'set-workdir',
                             'file context and sibling import', 'inline collaborative edit', 'stdin',
                             'error exit and source filename', 'job monitoring', 'ODB field summary', 'viewport file'],
                  'events': events, 'artifacts': str(directory)}
    except Exception as exc:
        report = {'ok': False, 'error': str(exc), 'events': events}
    (directory / 'live-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'events'}, ensure_ascii=False))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
